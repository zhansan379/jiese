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

_TREE_CACHE: list[dict] = []   # 启用的叶子节点


def refresh_intent_tree() -> None:
    global _TREE_CACHE
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, kind, action, description, examples, gen_prompt "
            "FROM intent_nodes WHERE enabled ORDER BY sort_order, code"
        )
        _TREE_CACHE = [
            {
                "code": r[0], "name": r[1], "kind": r[2],
                "action": r[3] if isinstance(r[3], dict) else json.loads(r[3] or "{}"),
                "description": r[4] or "",
                "examples": [e for e in (r[5] or "").split("|") if e],
                "gen_prompt": r[6] or "",
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
    """分类：LLM 叶子打分 → 兜底默认全库。"""
    if not _TREE_CACHE:
        refresh_intent_tree()
    hit = _llm_classify(question)
    if hit:
        return hit
    return Intent(code="kb_all", source="default", action=dict(DEFAULT_ACTION))
