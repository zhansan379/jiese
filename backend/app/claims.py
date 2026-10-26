"""Claim 共识层（实现计划功能一：docs/实现计划-共识层-实体-画像.md）。

把"几十位戒友复述的相同经验"从分散 chunk 聚合为带支持度的经验点（Claim）：
- 离线：scripts/build_claims.py（LLM 抽取断言 → bge-m3 嵌入 → 贪心质心聚簇 → 落库）
- 运行时：retriever.retrieve() 末尾对 top-k chunk 批查 claim_evidence，命中共识点
  附到 Citation.claims；本模块把引用命中的共识点聚合为生成端「共识信号」段：
      共识信号（可信度参考）：
      - 断念口诀需每日练习形成条件反射 —— 飞翔及 27 位戒友均强调（覆盖2本书 20 处，见摘录 [3][5]）
- 过滤：support_chunks < MIN_CLAIM_SUPPORT 的簇不参与运行时（噪声，保留在库供人工复核）
"""
from __future__ import annotations

from .db import conn_ctx

# 运行时共识门槛：支撑 chunk 数低于该值的簇不进运行时
MIN_CLAIM_SUPPORT = 3
# 生成端共识信号段最多列出的共识点数 / 每条最多标注的摘录数
MAX_SIGNALS = 5
MAX_CITES_PER_SIGNAL = 4


def load_claims_for_chunks(chunk_ids: list[str]) -> dict[str, list[dict]]:
    """批查 chunk → 命中的启用共识点（一次 SQL）。表未建/查询失败返回空。"""
    if not chunk_ids:
        return {}
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ce.chunk_id, c.id, c.text, c.support_authors, c.support_chunks,"
                " c.authors, c.books FROM claim_evidence ce JOIN claims c ON c.id = ce.claim_id"
                " WHERE ce.chunk_id = ANY(%s) AND c.enabled AND c.support_chunks >= %s",
                (chunk_ids, MIN_CLAIM_SUPPORT),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001  共识层未构建/暂不可用时静默降级
        print(f"[claims] 共识查询失败（未构建可忽略）: {e}")
        return {}
    by_chunk: dict[str, list[dict]] = {}
    for r in rows:
        by_chunk.setdefault(r["chunk_id"], []).append({
            "id": r["id"], "text": r["text"],
            "support_authors": r["support_authors"], "support_chunks": r["support_chunks"],
            "authors": r["authors"] or [], "books": r["books"] or {},
        })
    return by_chunk


def build_consensus_signal(citations) -> str:
    """聚合各引用命中的共识点 → 生成端「共识信号」段（去重、按支持度排序、标注摘录编号）。

    摘录编号 = 引用在 citations 中的 1-based 序号，与生成端摘录 [n] 编号一致，
    LLM 据此保留对应引用。无命中返回空串。
    """
    by_id: dict[int, dict] = {}
    for i, c in enumerate(citations, 1):
        for cl in c.claims:
            entry = by_id.get(cl["id"])
            if entry is None:
                by_id[cl["id"]] = {**cl, "cites": [i]}
            elif i not in entry["cites"]:
                entry["cites"].append(i)
    if not by_id:
        return ""
    signals = sorted(by_id.values(), key=lambda x: -x["support_authors"])[:MAX_SIGNALS]
    lines = []
    for cl in signals:
        authors = cl["authors"] if isinstance(cl["authors"], list) else []
        lead = authors[0] if authors else "戒友"
        books = cl["books"] if isinstance(cl["books"], dict) else {}
        if books:
            coverage = f"覆盖{len(books)}本书 {sum(books.values())} 处"
        else:
            coverage = f"覆盖 {cl['support_chunks']} 处"
        cites = "".join(f"[{i}]" for i in cl["cites"][:MAX_CITES_PER_SIGNAL])
        lines.append(f"- {cl['text']} —— {lead}及 {cl['support_authors']} 位戒友均强调"
                     f"（{coverage}，见摘录 {cites}）")
    return "共识信号（可信度参考）：\n" + "\n".join(lines)
