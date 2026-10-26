"""Claim 共识层离线构建管线（实现计划功能一）。

① 抽取：全量 chunk → LLM 抽取 0~3 条原子经验断言（Qwen3-32B, temp 0.1，并发 N 路，
   结果落 claim_extractions 缓存表，断点续跑：命中缓存且文本哈希一致则跳过 LLM）
② 嵌入：断言文本调 bge-m3（批量）
③ 聚簇：贪心质心聚类——按序取断言，与已有簇心余弦 ≥0.85 归入（更新簇心为均值），否则新建簇
④ 落库：簇 → claims（text=簇内最长断言，authors=来源 chunk 作者去重，books 分布，
   support_chunks=簇大小），成员写 claim_evidence；全量重建（聚类是确定性的）
⑤ 过滤：support_chunks < 3 的簇保留在库但不参与运行时（运行时门槛见 app/claims.py）

用法（backend 目录下）：
    python scripts/build_claims.py                  # 全量构建（增量抽取缓存 + 全量重聚簇）
    python scripts/build_claims.py --limit 200      # 只处理前 200 个 chunk（调试）
    python scripts/build_claims.py --concurrency 8  # 抽取并发（默认 8）
    python scripts/build_claims.py --threshold 0.85 # 归簇余弦阈值
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402

EXTRACT_SYSTEM = (
    "你是经验断言抽取器。从戒色书籍摘录中抽取 0~3 条\"可被他人复述验证的原子经验断言\"：\n"
    "- 只抽经验/方法/规律类陈述（如：断念口诀需每日练习形成条件反射；固肾功可对治频遗）\n"
    "- 跳过纯案例叙述、纯闲聊、诗歌引文、祝福语\n"
    "- 每条一句话、40 字以内、语义完整、不依赖上下文\n"
    '只输出 JSON 数组 [{"claim": "..."}]，无可抽经验输出 []，不要输出任何其他内容。'
)

_JSON_ARR_RE = re.compile(r"\[.*\]", re.S)


def _extract_one(chunk_id: str, text: str, model: str) -> list[str] | None:
    """单 chunk 的 LLM 断言抽取（线程内同步调用）。

    返回断言列表（可能为空 = 该 chunk 确无可抽经验）；调用失败（含限流重试耗尽）
    返回 None —— None 不写缓存，下次运行自动补抽。429 限流按退避重试。
    """
    from openai import OpenAI

    client = OpenAI(base_url=settings.openai_base_url, api_key=settings.openai_api_key, timeout=120)
    extra = {"enable_thinking": False} if "Qwen3" in model else None
    resp = None
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": text[:1200]},
                ],
                temperature=0.1,
                max_tokens=300,
                extra_body=extra,
            )
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                print(f"[extract] {chunk_id} 失败（重试耗尽，不缓存）: {e}")
                return None
            time.sleep((attempt + 1) * 15)
    raw = (resp.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    m = _JSON_ARR_RE.search(raw)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        claim = (item.get("claim") or "").strip() if isinstance(item, dict) else ""
        if 8 <= len(claim) <= 80 and claim not in out:
            out.append(claim)
    return out[:3]


def _stage_extract(conn, chunks: list[dict], concurrency: int, model: str) -> dict[str, list[str]]:
    """① 抽取（带 claim_extractions 缓存，断点续跑；失败不缓存，下次运行补抽）。

    返回 chunk_id → [断言]（仅含成功抽取的 chunk）。
    """
    result: dict[str, list[str]] = {}
    todo: list[dict] = []
    with conn.cursor() as cur:
        cur.execute("SELECT chunk_id, text_hash, claims FROM claim_extractions")
        cache = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    for c in chunks:
        h = hashlib.md5(c["text"].encode("utf-8")).hexdigest()
        c["hash"] = h
        hit = cache.get(c["id"])
        if hit and hit[0] == h and isinstance(hit[1], list) and hit[1]:
            result[c["id"]] = [x for x in hit[1] if isinstance(x, str) and x.strip()]
        else:
            todo.append(c)
    if todo:
        print(f"[1/4] 抽取：缓存命中 {len(result)}，待抽取 {len(todo)}（并发 {concurrency}，模型 {model}）")
    done = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_extract_one, c["id"], c["text"], model): c for c in todo}
        for fut in as_completed(futures):
            c = futures[fut]
            claims = fut.result()
            if claims is None:
                failed += 1
                continue
            result[c["id"]] = claims
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO claim_extractions (chunk_id, text_hash, claims)"
                    " VALUES (%s,%s,%s) ON CONFLICT (chunk_id) DO UPDATE"
                    " SET text_hash=EXCLUDED.text_hash, claims=EXCLUDED.claims",
                    (c["id"], c["hash"], json.dumps(claims, ensure_ascii=False)),
                )
            conn.commit()
            done += 1
            if done % 100 == 0 or done + failed == len(todo):
                rate = done / max(1e-9, time.time() - t0)
                eta = (len(todo) - done - failed) / rate / 60 if rate > 0 else 0
                print(f"    {done + failed}/{len(todo)}（{rate:.1f} chunk/s，"
                      f"失败 {failed}，预计剩余 {eta:.0f} 分钟）")
    total = sum(len(v) for v in result.values())
    print(f"[1/4] 抽取完成：{total} 条断言" + (f"（{failed} 个 chunk 失败未缓存，重跑可补抽）" if failed else ""))
    return result


def _stage_embed(claims: list[str]) -> np.ndarray:
    """② 断言嵌入（bge-m3，批量）。"""
    from openai import OpenAI

    print(f"[2/4] 嵌入：{len(claims)} 条断言，模型 {settings.embedding_model}")
    client = OpenAI(base_url=settings.emb_base, api_key=settings.emb_key, timeout=120)
    vecs: list[list[float]] = []
    batch = 64
    t0 = time.time()
    for i in range(0, len(claims), batch):
        part = claims[i:i + batch]
        for attempt in range(3):
            try:
                resp = client.embeddings.create(model=settings.embedding_model, input=part)
                vecs.extend(d.embedding for d in resp.data)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if (i // batch) % 20 == 0:
            print(f"    {i + len(part)}/{len(claims)}")
    mat = np.array(vecs, dtype=np.float32)
    mat /= np.maximum(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9)
    print(f"[2/4] 嵌入完成（{time.time() - t0:.0f}s）")
    return mat


def _stage_cluster(mat: np.ndarray, threshold: float) -> list[list[int]]:
    """③ 贪心质心聚类（向量化：质心矩阵一次 matmul 算全部余弦）。返回簇 → 断言下标列表。"""
    print(f"[3/4] 聚类：阈值余弦 ≥{threshold}")
    dim = mat.shape[1]
    sums = np.zeros((0, dim), dtype=np.float32)       # 簇内向量和（方向 = 簇心方向）
    cents_n = np.zeros((0, dim), dtype=np.float32)    # 归一化簇心（比较用）
    members: list[list[int]] = []
    t0 = time.time()
    for i in range(mat.shape[0]):
        v = mat[i]
        if cents_n.shape[0]:
            sims = cents_n @ v
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                sums[best] += v
                n = float(np.linalg.norm(sums[best])) or 1.0
                cents_n[best] = sums[best] / n
                members[best].append(i)
                continue
        sums = np.vstack([sums, v])
        cents_n = np.vstack([cents_n, v])
        members.append([i])
    sizes = sorted((len(m) for m in members), reverse=True)
    print(f"[3/4] 聚类完成：{len(members)} 簇（top 簇大小 {sizes[:10]}，"
          f"≥3 条的簇 {sum(1 for s in sizes if s >= 3)}，耗时 {time.time() - t0:.0f}s）")
    return members


def _stage_persist(conn, chunks_by_id: dict[str, dict], flat_claims: list[dict],
                   members: list[list[int]], mat: np.ndarray) -> None:
    """④ 落库：claims + claim_evidence（全量重建）。"""
    print("[4/4] 落库 claims / claim_evidence ...")
    with conn.cursor() as cur:
        cur.execute("TRUNCATE claims, claim_evidence RESTART IDENTITY")
        for ci, member in enumerate(members):
            texts = [flat_claims[i]["claim"] for i in member]
            chunk_ids = sorted({flat_claims[i]["chunk_id"] for i in member})
            authors = sorted({chunks_by_id[c]["author"] for c in chunk_ids if chunks_by_id[c].get("author")})
            books = Counter(chunks_by_id[c]["book_id"] for c in chunk_ids)
            text = max(texts, key=len)
            centroid = mat[member].mean(axis=0)
            cur.execute(
                "INSERT INTO claims (text, centroid, support_authors, support_chunks, authors, books)"
                " VALUES (%s,%s::vector,%s,%s,%s,%s) RETURNING id",
                (text, "[" + ",".join(f"{x:.6f}" for x in centroid.tolist()) + "]",
                 len(authors), len(member),
                 json.dumps(authors, ensure_ascii=False),
                 json.dumps(dict(books), ensure_ascii=False)),
            )
            claim_id = cur.fetchone()[0]
            cur.executemany(
                "INSERT INTO claim_evidence (claim_id, chunk_id) VALUES (%s,%s)"
                " ON CONFLICT DO NOTHING",
                [(claim_id, c) for c in chunk_ids],
            )
    conn.commit()
    strong = sum(1 for m in members if len(m) >= 3)
    print(f"[4/4] 落库完成：{len(members)} 簇（运行时参与 {strong} 簇）")


def main() -> None:
    ap = argparse.ArgumentParser(description="Claim 共识层离线构建")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 chunk（调试用）")
    ap.add_argument("--concurrency", type=int, default=8, help="抽取并发数")
    ap.add_argument("--threshold", type=float, default=0.85, help="归簇余弦阈值")
    args = ap.parse_args()

    if not (settings.llm_enabled and settings.emb_base and settings.embedding_model):
        print("需要配置 OPENAI_*（LLM 抽取）与 EMBEDDING_*（bge-m3）")
        sys.exit(1)

    conn = psycopg2.connect(**settings.dsn)
    try:
        with conn.cursor() as cur:
            sql = ("SELECT id, book_id, author, text FROM chunks WHERE dup_of IS NULL ORDER BY seq")
            if args.limit:
                sql += f" LIMIT {int(args.limit)}"
            cur.execute(sql)
            chunks = [
                {"id": r[0], "book_id": r[1], "author": r[2], "text": r[3]}
                for r in cur.fetchall()
            ]
        print(f"共 {len(chunks)} 个 chunk 待处理")
        by_id = {c["id"]: c for c in chunks}

        extracted = _stage_extract(conn, chunks, args.concurrency, settings.llm_model)
        flat: list[dict] = []
        for cid, claims in extracted.items():
            for claim in claims:
                flat.append({"chunk_id": cid, "claim": claim})
        if not flat:
            print("无断言可聚类（检查 LLM 配置或抽取结果）")
            return
        claim_texts = [f["claim"] for f in flat]
        mat = _stage_embed(claim_texts)
        members = _stage_cluster(mat, args.threshold)
        _stage_persist(conn, by_id, flat, members, mat)

        # 预览 top 簇
        print("\nTop 共识簇预览（按簇大小）:")
        for member in sorted(members, key=len, reverse=True)[:8]:
            texts = [flat[i]["claim"] for i in member]
            cids = {flat[i]["chunk_id"] for i in member}
            authors = {by_id[c]["author"] for c in cids if by_id[c].get("author")}
            preview = max(texts, key=len)
            print(f"  [{len(member)} chunks / {len(authors)} 位作者] {preview}")
            for t in sorted(set(texts), key=len, reverse=True)[1:4]:
                print(f"      · {t}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
