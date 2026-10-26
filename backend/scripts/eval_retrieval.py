"""检索质量评测：BM25 基线 vs 各候选 embedding 模型（向量单路 / 混合）。

评测集来自两本书的真实问题形态，章节级期望（关键词命中即算命中）。
指标：hit@3 / hit@6 / MRR。

用法（backend 目录下，先运行 embed_index.py 生成各模型索引）：
    python scripts/eval_retrieval.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import psycopg2
from openai import OpenAI

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.entities import ENTITY_BOOST, load_chunk_entities, match_entities  # noqa: E402
from app.glossary import expand_tokens, tokenize  # noqa: E402
from app.indexer import BM25  # noqa: E402

RRF_K = 60

# (查询, 期望章节关键词集合 —— 命中任一即算正确)
EVAL_SET = [
    ("滑精很频繁怎么办", ["控遗之道"]),
    ("一到周末就忍不住破戒", ["周末破戒", "戒色十规"]),
    ("断念口诀具体怎么用", ["断念口诀", "俞净意公"]),
    ("社恐能恢复吗", ["社恐"]),
    ("脱发还能长回来吗", ["脱发"]),
    ("怎么才能不做淫梦", ["梦中撸管"]),
    ("戒了很久突然破了很绝望", ["戒色意识"]),
    ("盖厚被子导致遗精", ["案例分析"]),
    ("前列腺炎怎么恢复", ["前列腺炎"]),
    ("熬夜久坐有什么危害", ["熬夜久坐"]),
    ("早泄能治好吗", ["早泄"]),
    ("痤疮一直好不了", ["痤疮"]),
    ("神经衰弱怎么康复", ["神衰"]),
    ("白头发黑眼圈怎么办", ["白发", "黑眼圈"]),
    ("意淫太严重了", ["YY终结者", "克服意淫"]),
    ("晨勃消失了正常吗", ["晨勃"]),
    ("什么是戒断反应", ["戒断反应"]),
    ("焦虑症惊恐发作", ["焦虑症"]),
]

CANDIDATES = [
    "BAAI/bge-m3",
    "BAAI/bge-large-zh-v1.5",
    "Qwen/Qwen3-Embedding-0.6B",
    "Qwen/Qwen3-Embedding-8B",
]


class EvalRetriever:
    """与 app/retriever.py 生产路径完全一致：同词表扩展、同 RRF、同查询文本构造。"""

    def __init__(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tokens, chapter_title FROM chunks WHERE dup_of IS NULL ORDER BY seq"
            )
            rows = cur.fetchall()
        self.ids = [r[0] for r in rows]
        self.chapters = [r[2] or "" for r in rows]
        token_lists = [json.loads(r[1]) for r in rows]
        self.bm25 = BM25(self.ids, token_lists)
        self.id2idx = {i: n for n, i in enumerate(self.ids)}
        # 实体加成（与生产 retrieve() 一致）：chunk_id → set(terms)
        try:
            self.chunk_entities = load_chunk_entities(conn)
        except Exception:  # noqa: BLE001  未打标（chunks.entities 列缺失）时降级
            self.chunk_entities = {}
        self.client = OpenAI(base_url=settings.emb_base, api_key=settings.emb_key, timeout=60)

    def load_vectors(self, npy_path: Path, ids_path: Path) -> None:
        self.vectors = np.load(npy_path)
        with open(ids_path, encoding="utf-8") as f:
            self.vector_ids = json.load(f)

    def query_vector(self, query: str) -> np.ndarray:
        text = " ".join(expand_tokens(tokenize(query)))
        resp = self.client.embeddings.create(model=self.model, input=[text])
        q = np.array(resp.data[0].embedding, dtype=np.float32)
        return q / (np.linalg.norm(q) or 1.0)

    def search(self, query: str, mode: str, k: int = 6) -> list[str]:
        expanded = expand_tokens(tokenize(query))
        bm25_hits = self.bm25.search(expanded, 24)
        if not bm25_hits:
            bm25_hits = self.bm25.search(tokenize(query), 24)

        vec_hits: list[tuple[str, float]] = []
        if mode in ("vector", "hybrid") and self.vectors is not None:
            mat = self.vec_matrix  # 已归一化
            sims = mat @ self.query_vector(query)
            order = np.argsort(-sims)[:24]
            vec_hits = [(self.vector_ids[i], float(sims[i])) for i in order]

        if mode == "bm25":
            ranked = bm25_hits
        elif mode == "vector":
            ranked = vec_hits
        else:
            rrf: dict[str, float] = {}
            for hits in (bm25_hits, vec_hits):
                for rank, (cid, _) in enumerate(hits):
                    rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            ranked = sorted(rrf.items(), key=lambda kv: -kv[1])
            # 实体加成（生产 retrieve() 同款：RRF 后、rerank 前，交集 ×1.2）
            query_entities = match_entities(query)
            if query_entities and self.chunk_entities:
                ranked = [
                    (cid, s * ENTITY_BOOST if self.chunk_entities.get(cid, set()) & query_entities else s)
                    for cid, s in ranked
                ]
                ranked.sort(key=lambda x: -x[1])
        return [cid for cid, _ in ranked[:k]]


def score(retriever: "EvalRetriever", mode: str) -> dict:
    hits3 = hits6 = 0
    mrr = 0.0
    for query, keywords in EVAL_SET:
        cids = retriever.search(query, mode, k=6)
        ranks = [
            n + 1 for n, cid in enumerate(cids)
            if any(kw in retriever.chapters[retriever.id2idx[cid]] for kw in keywords)
        ]
        if ranks:
            hits6 += 1
            if ranks[0] <= 3:
                hits3 += 1
            mrr += 1.0 / ranks[0]
    n = len(EVAL_SET)
    return {"hit@3": hits3 / n, "hit@6": hits6 / n, "MRR": mrr / n}


def main() -> None:
    conn = psycopg2.connect(**settings.dsn)
    r = EvalRetriever(conn)
    conn.close()

    # BM25 基线（与模型无关，只算一次）
    print(f"\n评测集: {len(EVAL_SET)} 题 | 候选: {len(CANDIDATES)} 模型")
    results = {"BM25 (基线)": score(r, "bm25")}

    vec_dir = settings.data_dir / "vectors"
    for model in CANDIDATES:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", model)
        npy, ids = vec_dir / f"{safe}.npy", vec_dir / f"{safe}.ids.json"
        if not npy.exists():
            print(f"[skip] {model}: 索引不存在（先运行 embed_index.py）")
            continue
        r.load_vectors(npy, ids)
        mat = r.vectors / np.linalg.norm(r.vectors, axis=1, keepdims=True)
        r.vec_matrix, r.model, r.vectors = mat, model, mat
        results[f"{model} 向量"] = score(r, "vector")
        results[f"{model} 混合"] = score(r, "hybrid")

    print(f"\n{'方案':<44}{'hit@3':>8}{'hit@6':>8}{'MRR':>8}")
    print("-" * 70)
    for name, s in sorted(results.items(), key=lambda kv: -kv[1]["MRR"]):
        print(f"{name:<44}{s['hit@3']:>8.1%}{s['hit@6']:>8.1%}{s['MRR']:>8.3f}")


if __name__ == "__main__":
    main()
