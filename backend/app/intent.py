"""意图识别（参照 ragent 树形意图）：LLM 叶子打分 + 动作路由 + 节点级提示词注入。

- 意图树存 PostgreSQL（intent_nodes 表），管理后台可编辑
- 分类：LLM 叶子打分（description/examples 塞 prompt，输出 [{code,score}]），
  失败/无命中兜底为默认全库检索（与 ragent 空意图兜底一致）
- 节点动作（action）：kb 路由（book_id）/ system 应答（固定话术直接流式返回）
- 节点 gen_prompt：该意图生效时注入生成阶段 system prompt 的风格指引（提示词注入）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import settings
from .db import conn_ctx


@dataclass
class Intent:
    code: str
    source: str          # llm | default
    action: dict         # {"type": "kb", "book_id": ...} | {"type": "system", "reply": ...}
    gen_prompt: str = ""  # 意图级提示词注入（生成阶段追加到 system）
    score: float = 0.0


DEFAULT_ACTION = {"type": "kb", "book_id": None}

_TREE_CACHE: list[dict] = []   # 启用的叶子节点（分组不参与打分）
_TREE_ENABLED = True          # 意图树总开关（app_settings.intent_tree_enabled）


def refresh_intent_enabled() -> None:
    global _TREE_ENABLED
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key='intent_tree_enabled'")
            row = cur.fetchone()
        _TREE_ENABLED = (row[0] == "1") if row else True
    except Exception:  # noqa: BLE001
        _TREE_ENABLED = True


def refresh_intent_tree() -> None:
    """加载启用的叶子节点：有子节点的视为分组不参与打分；分组停用则其子树整体禁用。"""
    global _TREE_CACHE, _TREE_ENABLED
    refresh_intent_enabled()
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, parent_code, kind, action, description, examples, gen_prompt, enabled "
            "FROM intent_nodes ORDER BY sort_order, code"
        )
        rows = [
            {
                "code": r[0], "name": r[1], "parent_code": r[2], "kind": r[3],
                "action": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}"),
                "description": r[5] or "",
                "examples": [e for e in (r[6] or "").split("|") if e],
                "gen_prompt": r[7] or "",
                "enabled": r[8],
            }
            for r in cur.fetchall()
        ]

    has_children = {r["parent_code"] for r in rows if r["parent_code"]}
    # 分组停用 → 整棵子树禁用
    disabled_groups = {r["code"] for r in rows if not r["enabled"]}
    def subtree_disabled(node):
        p = node["parent_code"]
        while p:
            if p in disabled_groups:
                return True
            nxt = next((x for x in rows if x["code"] == p), None)
            p = nxt["parent_code"] if nxt else None
        return False

    _TREE_CACHE = [
        r for r in rows
        if r["code"] not in has_children  # 有子节点 = 分组，不参与打分
        and r["enabled"]                  # 叶子自身启用
        and not subtree_disabled(r)       # 且不在停用分组的子树里
    ]


def _build_prompt() -> str:
    lines = []
    for n in _TREE_CACHE:
        lines.append(f"- code={n['code']}")
        lines.append(f"  name={n['name']}  type={'SYSTEM' if n['kind'] == 'system' else 'KB'}")
        if n["description"]:
            lines.append(f"  description={n['description']}")
        if n["examples"]:
            lines.append(f"  examples={' / '.join(n['examples'])}")
        lines.append("")
    return (
        "你是意图分类器。根据用户问题为下列意图节点打分（0到1的小数），"
        '只输出 JSON 数组，格式 [{"code":"...","score":0.9}]，按分数降序，不要输出任何其他内容。\n'
        "规则：问题明确属于某一意图才给高分（>0.7）；与所有意图都无关的闲聊或外部问题，"
        "给 SYSTEM 类节点打高分。\n\n候选意图：\n" + "\n".join(lines)
    )


def _llm_classify(question: str) -> Intent | None:
    """LLM 叶子打分（ragent 模式）：temperature 0.1，失败返回 None 走兜底。"""
    try:
        from openai import OpenAI
        client = OpenAI(base_url=settings.openai_base_url, api_key=settings.openai_api_key, timeout=20)
        extra = {"enable_thinking": False} if "Qwen3" in settings.llm_model else None
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": _build_prompt()},
                {"role": "user", "content": question},
            ],
            temperature=0.1,
            max_tokens=300,
            extra_body=extra,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
        m = re.search(r"\[.*\]", raw, re.S)
        if not m:
            return None
        scores = json.loads(m.group(0))
        scores.sort(key=lambda x: -float(x.get("score", 0)))
        by_code = {n["code"]: n for n in _TREE_CACHE}
        for s in scores:
            node = by_code.get(s.get("code"))
            if node and float(s.get("score", 0)) >= 0.5:
                return Intent(code=node["code"], source="llm",
                              action=node["action"] or DEFAULT_ACTION,
                              gen_prompt=node["gen_prompt"],
                              score=float(s["score"]))
    except Exception as e:  # noqa: BLE001
        print(f"[intent] LLM 打分失败，走默认意图: {e}")
    return None


def classify(question: str) -> Intent:
    """分类：规则快通道（寒暄问候，独立于意图树开关）→ 意图树 LLM 叶子打分 → 兜底默认全库。

    规则快通道必须最先执行：reranker 对"你好"这类泛化问候会给出虚高分数（实测 top 0.85），
    阈值过滤拦不住，问候语必须在检索之前就被识别并短路。
    """
    hit = match_chitchat(question)
    if hit:
        return hit
    if not _TREE_CACHE:
        refresh_intent_tree()
    if not _TREE_ENABLED:
        return Intent(code="kb_all", source="default", action=dict(DEFAULT_ACTION))
    hit = _llm_classify(question)
    if hit:
        return hit
    return Intent(code="kb_all", source="default", action=dict(DEFAULT_ACTION))


# ---- 寒暄规则快通道（零成本、确定性，不依赖意图树/LLM）----
_CHITCHAT_RULES = [
    (re.compile(r"^(你好|您好|嗨|哈喽|halo|hello|hi|在吗|在么)[呀啊吧呢嘛!！。~～?\s]*$", re.I),
     "你好！我是基于《戒为良药》与《戒者录》两本书的戒色知识助手——"
     "可以帮你查症状恢复方法（频遗、脱发、社恐、神经症等）、戒色实战方法（断念、固肾功等）、"
     "以及上百位戒友的真实经历。有什么想问的，直接说就行。"),
    (re.compile(r"^(谢谢|多谢|感谢|辛苦了|太感谢)[你您]*[啦呀了哦!！。~～\s]*$"),
     "不客气，能帮到你我很高兴。戒色路上有任何问题随时来问，坚持就是胜利！"),
    (re.compile(r"^(再见|拜拜|晚安|睡了|回见)[啦呀哈咯!！。~～\s]*$"),
     "再见！记得保持警惕、好好休息。戒色只有进行时，没有完成时——随时回来。"),
    (re.compile(r"^(你是谁|你叫什么|你是干嘛的?|你能做什么|你会什么|介绍一下你自己|help)[?？。!\！~～\s]*$", re.I),
     "我是戒色知识助手：知识库来自《戒为良药》（飞翔的 140 章方法论）和《戒者录》（100 多位戒友的真实经历），"
     "回答都会附带原文出处，可以点击跳转到书中对应位置。试试问我具体问题，比如“频遗怎么办”。"),
    (re.compile(r"^(好的?|嗯+|哦+|噢+|收到|了解|明白|知道了?|ok|okay)[吧呗啦!！。~～\s]*$", re.I),
     "好的。有新的问题随时问我。"),
]


def match_chitchat(question: str) -> Intent | None:
    """寒暄/问候/致谢/告别/身份询问：仅匹配短消息，避免误伤正常提问。"""
    q = question.strip()
    if not q or len(q) > 12:
        return None
    for pat, reply in _CHITCHAT_RULES:
        if pat.match(q):
            return Intent(code="chitchat", source="rule",
                          action={"type": "system", "reply": reply}, score=1.0)
    return None
