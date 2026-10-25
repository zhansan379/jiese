"""内存 BM25 (Okapi) 索引。构建期已把 tokens 存入 SQLite，启动时直接加载重建，秒级完成。"""
from __future__ import annotations

import math
from collections import Counter


class BM25:
    def __init__(self, doc_ids: list[str], token_lists: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_ids = doc_ids
        self.n = len(doc_ids)
        self.doc_len = [len(t) for t in token_lists]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf: list[Counter] = [Counter(t) for t in token_lists]
        df: Counter = Counter()
        for toks in token_lists:
            df.update(set(toks))
        self.idf = {
            term: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for term, d in df.items()
        }

    def search(self, query_tokens: list[str], top_k: int = 24) -> list[tuple[str, float]]:
        if not self.n or not query_tokens:
            return []
        scores: dict[int, float] = {}
        qtf = Counter(t for t in query_tokens if t in self.idf)
        for term, q_w in qtf.items():
            idf = self.idf[term]
            for i in range(self.n):
                f = self.tf[i].get(term)
                if not f:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[i] = scores.get(i, 0.0) + idf * (f * (self.k1 + 1) / denom) * (1 + 0.1 * (q_w - 1))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [(self.doc_ids[i], s) for i, s in ranked]
