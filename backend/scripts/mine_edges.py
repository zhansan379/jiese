"""受限关系挖掘（实现计划功能二）：模式匹配为主（免费、可控）+ 章节标题强信号。

- 对治边（方法—对治→问题）：「对治|克服|摆脱|彻底解决」连接的 (方法, 症状) 对
  ① 连接词两侧各出现一端：固肾功对治频遗 / 克服社恐的方法是观心
  ② 连接词紧跟症状、句内另有方法实体：对治频遗的方法：固肾功、睡前拉筋
- 导致边（因素—导致→症状）：「导致|造成|引起」连接的 (因素, 症状) 对，同上两种模式
- 章节标题强信号：标题含症状的章内出现的方法实体 → 对治边（如第 3 章频遗 → 固肾功）
- evidence 记录来源 chunk_ids；全部落库（管理端可复核删除），运行时仅用 evidence ≥ 2 的边

用法（backend 目录下，先完成 seed_terms + backfill_entities）：
    python scripts/mine_edges.py
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import init_pool  # noqa: E402
from app.entities import REL_CAUSE, REL_TREAT, match_entities_typed  # noqa: E402

_SENT_SPLIT = "\n。！？；!?;"

TREAT_KW = ("对治", "克服", "摆脱", "彻底解决")
CAUSE_KW = ("导致", "造成", "引起")

MAX_EVIDENCE_PER_EDGE = 30


def _sentences(text: str):
    buf: list[str] = []
    for ch in text:
        if ch in _SENT_SPLIT:
            if buf:
                yield "".join(buf)
                buf = []
        else:
            buf.append(ch)
    if buf:
        yield "".join(buf)


def _find_kw(sentence: str, keywords: tuple[str, ...]) -> int:
    """任一关键词的最早出现位置，无则 -1。"""
    best = -1
    for kw in keywords:
        pos = sentence.find(kw)
        if pos >= 0 and (best < 0 or pos < best):
            best = pos
    return best


def mine() -> None:
    init_pool()
    conn = psycopg2.connect(**settings.dsn)
    t0 = time.time()
    try:
        edges: dict[tuple[str, str, str], set[str]] = defaultdict(set)

        def add_edge(src: str, rel: str, dst: str, chunk_id: str) -> None:
            if src and dst and src != dst:
                bucket = edges[(src, rel, dst)]
                if len(bucket) < MAX_EVIDENCE_PER_EDGE:
                    bucket.add(chunk_id)

        with conn.cursor() as cur:
            cur.execute("SELECT id, text FROM chunks ORDER BY seq")
            chunks = cur.fetchall()

        # ---- ① 连接词两侧配对 + ② 连接词紧跟问题实体 ----
        pattern_hits = 0
        for cid, text in chunks:
            for sent in _sentences(text):
                typed = match_entities_typed(sent)
                methods = typed.get("方法", [])
                factors = typed.get("因素", [])
                symptoms = typed.get("症状", [])
                if not symptoms or not (methods or factors):
                    continue

                pos_t = _find_kw(sent, TREAT_KW)
                if pos_t >= 0 and methods:
                    pattern_hits += 1
                    lt = match_entities_typed(sent[:pos_t])
                    rt = match_entities_typed(sent[pos_t:])
                    for m in lt.get("方法", []):
                        for s in rt.get("症状", []):
                            add_edge(m, REL_TREAT, s, cid)
                    for m in rt.get("方法", []):
                        for s in lt.get("症状", []):
                            add_edge(m, REL_TREAT, s, cid)
                    # 「对治X的方法：固肾功…」：连接词紧后的问题实体 × 句内任意方法
                    at = match_entities_typed(sent[pos_t:pos_t + 12])
                    for s in at.get("症状", []):
                        for m in methods:
                            add_edge(m, REL_TREAT, s, cid)

                pos_c = _find_kw(sent, CAUSE_KW)
                if pos_c >= 0 and factors:
                    pattern_hits += 1
                    lt = match_entities_typed(sent[:pos_c])
                    rt = match_entities_typed(sent[pos_c:])
                    for f in lt.get("因素", []):
                        for s in rt.get("症状", []):
                            add_edge(f, REL_CAUSE, s, cid)
                    for f in rt.get("因素", []):
                        for s in lt.get("症状", []):
                            add_edge(f, REL_CAUSE, s, cid)

        # ---- ③ 章节标题强信号：标题含症状的章内方法实体 → 对治边 ----
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, book_id, chapter_title, text FROM chunks ORDER BY seq"
            )
            rows = cur.fetchall()
        chapter_members: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for cid, book_id, chapter, text in rows:
            if chapter:
                chapter_members[(book_id, chapter)].append((cid, text))
        title_signal_hits = 0
        for (_book_id, chapter), members in chapter_members.items():
            title_syms = match_entities_typed(chapter).get("症状", [])
            if not title_syms:
                continue
            for cid, text in members:
                for m in match_entities_typed(text).get("方法", []):
                    for s in title_syms:
                        add_edge(m, REL_TREAT, s, cid)
                        title_signal_hits += 1

        # ---- 落库（全量重建：挖掘是当前语料的确定性推导；管理端删除后重跑会复原）----
        with conn.cursor() as cur:
            cur.execute("TRUNCATE term_edges")
            ordered = sorted(edges.items(), key=lambda kv: -len(kv[1]))
            for (src, rel, dst), evidence in ordered:
                cur.execute(
                    "INSERT INTO term_edges (src_term, rel, dst_term, evidence)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (src_term, rel, dst_term) DO UPDATE SET evidence = EXCLUDED.evidence",
                    (src, rel, dst, json.dumps(sorted(evidence), ensure_ascii=False)),
                )
        conn.commit()

        by_rel = defaultdict(int)
        for (_s, rel, _d), ev in edges.items():
            by_rel[rel] += 1
        strong = sum(1 for ev in edges.values() if len(ev) >= 2)
        print(f"模式命中句 {pattern_hits}，标题信号边 {title_signal_hits}，"
              f"共 {len(edges)} 条边（对治 {by_rel[REL_TREAT]} / 导致 {by_rel[REL_CAUSE]}），"
              f"evidence≥2 的边 {strong} 条，落库完成，耗时 {time.time() - t0:.1f}s")
        print("\nTop 对治边（按证据数，evidence≥2）:")
        shown = 0
        for (src, rel, dst), ev in ordered:
            if rel == REL_TREAT and len(ev) >= 2:
                print(f"  {src} —对治→ {dst}  ({len(ev)} chunks)")
                shown += 1
                if shown >= 30:
                    break
    finally:
        conn.close()


if __name__ == "__main__":
    mine()
