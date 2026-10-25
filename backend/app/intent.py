"""意图识别（参照 ragent 的树形意图架构，适配本项目规模）。

- 意图树存 PostgreSQL（intent_nodes 表），管理后台可编辑
- 分类两级：规则快通道（intent_rules 表，情绪正则，零延迟）→
  LLM 叶子打分（把叶子节点 description/examples 塞 prompt，输出 [{code,score}]）
- 节点动作（action）：kb 路由（book_id）/ system 应答（chitchat 直接回复不检索）；
  情绪为叠加标志（注入个人叙事），与 kb 路由正交
- 任何失败兜底为默认意图（全库检索），与 ragent 的空意图兜底一致
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import settings
from .db import conn_ctx

# ---------------- 情绪规则（intent_rules 表，管理端可编辑；种子见 DEFAULT_RULES） ----------------

# 首次建库时的种子规则（builtin=1，不可删除、可停用）
DEFAULT_RULES = [
    r"绝望", r"崩溃", r"撑不下去", r"撑不住", r"坚持不下",
    r"没用", r"废人", r"自卑", r"恨自己", r"后悔死",
    r"想放弃", r"想死", r"活着没意思", r"破罐破摔", r"救救",
    r"心态崩", r"好累", r"太累了", r"又破了", r"一次又一次",
    r"戒不掉", r"屡戒屡破", r"怪圈", r"失败",
]

_CACHE: list[re.Pattern] = []


def refresh_intent_rules() -> None:
    """从库重载启用的规则（管理端增删改后调用）。"""
    global _CACHE
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT pattern FROM intent_rules WHERE enabled")
        _CACHE = [re.compile(r[0]) for r in cur.fetchall()]


# ---------------- 意图树（intent_nodes 表） ----------------

@dataclass
class Intent:
    code: str
    source: str          # rule | llm | default
    action: dict         # {"type": "kb", "book_id": ...} | {"type": "system", "reply": ...}
    score: float = 0.0
    emotion: bool = False


DEFAULT_ACTION = {"type": "kb", "book_id": None}

_TREE_CACHE: list[dict] = []   # 启用的叶子节点


def refresh_intent_tree() -> None:
    global _TREE_CACHE
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, kind, action, description, examples "
            "FROM intent_nodes WHERE enabled ORDER BY sort_order, code"
        )
        _TREE_CACHE = [
            {
                "code": r[0], "name": r[1], "kind": r[2],
                "action": r[3] if isinstance(r[3], dict) else json.loads(r[3] or "{}"),
                "description": r[4] or "",
                "examples": [e for e in (r[5] or "").split("|") if e],
            }
            for r in cur.fetchall()
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
        "只输出 JSON 数组，格式 [{\"code\":\"...\",\"score\":0.9}]，按分数降序，不要输出任何其他内容。\n"
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
                              score=float(s["score"]))
    except Exception as e:  # noqa: BLE001
        print(f"[intent] LLM 打分失败，走默认意图: {e}")
    return None


def classify(question: str) -> Intent:
    """完整分类：规则（情绪快通道）→ LLM 树打分 → 默认全库。"""
    if not _TREE_CACHE:
        refresh_intent_tree()
    if not _CACHE:
        try:
            refresh_intent_rules()
        except Exception:  # noqa: BLE001
            _CACHE.append(re.compile(r"$^"))  # 空占位，避免每次查库

    # ① 规则快通道：命中情绪正则 → 情绪标志置位，跳过 LLM（情绪求助按全库检索+叙事注入）
    for pat in _CACHE:
        if pat.search(question):
            return Intent(code="emotion", source="rule",
                          action=dict(DEFAULT_ACTION), emotion=True)

    # ② LLM 叶子打分
    hit = _llm_classify(question)
    if hit:
        return hit

    # ③ 兜底：默认全库（与 ragent 空意图兜底一致）
    return Intent(code="kb_all", source="default", action=dict(DEFAULT_ACTION))


def detect_intent(text: str) -> str:
    """兼容旧接口：'emotional' | 'normal'。"""
    if not _CACHE:
        try:
            refresh_intent_rules()
        except Exception:  # noqa: BLE001
            return "normal"
    return "emotional" if any(p.search(text) for p in _CACHE) else "normal"
