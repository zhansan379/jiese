"""仅生成向量索引（不重建知识库）：读取 PostgreSQL 中的 chunks，调用 embedding API，
保存到 data/vectors/<model>.npy + <model>.ids.json，供评测与生产切换。

用法（backend 目录下）：
    python scripts/embed_index.py BAAI/bge-m3
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import psycopg2
from openai import OpenAI

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402

BATCH = 32
OUT_DIR = settings.data_dir / "vectors"


def safe_name(model: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", model)


def main(model: str) -> None:
    client = OpenAI(base_url=settings.emb_base, api_key=settings.emb_key, timeout=60)
    conn = psycopg2.connect(**settings.dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, tokens FROM chunks WHERE dup_of IS NULL ORDER BY seq")
        rows = cur.fetchall()
    conn.close()

    texts = [(" ".join(json.loads(t)) or "") for _, t in rows]
    ids = [i for i, _ in rows]
    print(f"[embed] {model}: {len(ids)} chunks, batch={BATCH}")

    vectors: list[list[float]] = []
    t0 = time.time()
    for i in range(0, len(texts), BATCH):
        part = texts[i:i + BATCH]
        for attempt in range(4):
            try:
                resp = client.embeddings.create(model=model, input=part)
                vectors.extend(d.embedding for d in resp.data)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                wait = 2 ** attempt * 2
                print(f"  batch {i}: {e} → 重试({wait}s)")
                time.sleep(wait)
        if i % (BATCH * 20) == 0:
            print(f"  {i}/{len(texts)} ({time.time() - t0:.0f}s)")

    matrix = np.array(vectors, dtype=np.float32)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.save(OUT_DIR / f"{safe_name(model)}.npy", matrix)
    (OUT_DIR / f"{safe_name(model)}.ids.json").write_text(
        json.dumps(ids), encoding="utf-8")
    print(f"[embed] 完成: shape={matrix.shape}, 耗时 {time.time() - t0:.0f}s → {OUT_DIR}")

    # 写入 PostgreSQL（pgvector）：作为生产检索的主存储
    if settings.emb_key and model == settings.embedding_model:
        from psycopg2.extras import execute_batch
        conn = psycopg2.connect(**settings.dsn)
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(1024)")
                rows = [("[" + ",".join(f"{x:.7g}" for x in vec) + ")", cid)
                        for cid, vec in zip(ids, matrix)]
                execute_batch(cur, "UPDATE chunks SET embedding = %s WHERE id = %s", rows, page_size=200)
            conn.commit()
            print(f"[embed] pgvector 写入完成（当前生产模型 {model}）")
        finally:
            conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
