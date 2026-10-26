"""全量 chunk 实体标注：terms + aliases 展开为模式字典，多模式匹配 chunk 正文（含章节/小节标题），
写 chunks.entities（形如 {"症状":["频遗"],"方法":["固肾功"]}）。

用法（backend 目录下，先运行 seed_terms.py 落库术语表）：
    python scripts/backfill_entities.py            # 全量打标（仅更新有变化的 chunk）
    python scripts/build_kb.py 后同样自动打标（构建挂钩）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psycopg2

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import init_pool  # noqa: E402
from app.entities import match_entities_typed  # noqa: E402


def backfill() -> None:
    init_pool()   # app.entities 的术语缓存经连接池读取
    conn = psycopg2.connect(**settings.dsn)
    t0 = time.time()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM terms WHERE enabled")
            n_terms = cur.fetchone()[0]
            print(f"启用术语 {n_terms} 条")
            cur.execute("SELECT id, text, chapter_title, section_title, entities FROM chunks ORDER BY seq")
            rows = cur.fetchall()
        updated = 0
        tagged = 0
        batch: list[tuple[str, str]] = []
        for cid, text, chapter, section, ents in rows:
            matched = match_entities_typed(f"{chapter or ''}\n{section or ''}\n{text}")
            payload = json.dumps(matched, ensure_ascii=False) if matched else "{}"
            current = ents if isinstance(ents, str) else json.dumps(ents, ensure_ascii=False)
            if current != payload:
                batch.append((payload, cid))
                updated += 1
            if matched:
                tagged += 1
        with conn.cursor() as cur:
            cur.executemany("UPDATE chunks SET entities=%s::jsonb WHERE id=%s", batch)
        conn.commit()
        print(f"打标完成：{len(rows)} chunks，有实体 {tagged}，更新 {updated}，"
              f"耗时 {time.time() - t0:.1f}s")
    finally:
        conn.close()


if __name__ == "__main__":
    backfill()
