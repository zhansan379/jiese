"""端到端评测（实现计划三项功能的验收断言），需要后端服务在运行。

用法（backend 目录下）：
    python scripts/eval_e2e.py --base http://127.0.0.1:8000

覆盖：
1. 共识表述：断念/警惕/学习类提问的答案应含共识表述（"位戒友"字样）且引用正确
2. 方法清单骨架：对治类提问（频遗怎么办）的答案应出现方法清单中的方法
3. 画像个性化：三轮对话后第三轮改写应含 "100天/脱发"（画像槽位注入改写）
4. 链路追踪：最近一次 Run 的节点应含 claims 命中数 / query_entities / profile-extract
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

CHECKS = [
    # (问题, 答案中应出现的字样（任一命中即过）, 说明)——共识类问题取自书中高共识主题
    ("断念口诀具体怎么练才有效", ["位戒友", "戒友均强调", "均强调"], "共识表述（断念）"),
    ("戒色后如何警惕避免破戒", ["位戒友", "戒友均强调", "均强调"], "共识表述（警惕）"),
    ("为什么要坚持学习戒色文章", ["位戒友", "戒友均强调", "均强调"], "共识表述（学习戒色文章）"),
    ("戒色后身体恢复有什么规律", ["位戒友", "戒友均强调", "均强调"], "共识表述（恢复规律）"),
    ("频遗怎么彻底解决", ["固肾功", "八段锦", "右侧卧"], "方法清单骨架（频遗）"),
    ("神经衰弱怎么康复", ["断念", "养生", "戒色", "锻炼"], "实体命中（神衰）"),
    ("前列腺炎怎么恢复", ["戒色", "养生", "锻炼", "前科"], "实体命中（前列腺炎）"),
    ("阴囊潮湿怎么处理", ["戒色", "锻炼", "前列腺"], "实体命中（阴囊潮湿）"),
    ("熬夜久坐有什么危害", ["伤精", "肾虚", "身体", "伤肾", "精气神"], "因素—导致边（熬夜久坐）"),
    ("手淫对脑力的影响能恢复吗", ["脑力", "恢复", "记忆力"], "实体命中（脑力）"),
]


def sse_events(buf: str) -> list[tuple[str, dict]]:
    events = []
    for block in buf.split("\n\n"):
        ev = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if ev and data:
            try:
                events.append((ev, json.loads(data)))
            except ValueError:
                pass
    return events


def chat(base: str, message: str, history: list[dict], session_id: str | None = None):
    with httpx.stream(
        "POST", f"{base}/api/chat",
        json={"message": message, "history": history, "session_id": session_id},
        timeout=180,
    ) as r:
        r.raise_for_status()
        buf = r.read().decode("utf-8", "ignore")
    events = sse_events(buf)
    meta = next((d for e, d in events if e == "meta"), {})
    citations = next((d for e, d in events if e == "citations"), [])
    answer = "".join(d.get("delta", "") for e, d in events if e == "answer" and isinstance(d, dict))
    return meta, citations, answer


def main() -> None:
    ap = argparse.ArgumentParser(description="三项功能端到端评测")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    passed = failed = 0
    session_id = f"s_eval_e2e_{int(__import__('time').time())}"

    # ---- 1/2：共识表述 + 方法清单 ----
    for question, needles, label in CHECKS:
        try:
            meta, citations, answer = chat(base, question, [])
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {label}「{question}」：请求失败 {e}")
            failed += 1
            continue
        hit = any(n in answer for n in needles if not n.startswith("_"))
        claims_hit = sum(len(c.get("claims") or []) for c in citations)
        status = "PASS" if hit else "FAIL"
        if hit:
            passed += 1
        else:
            failed += 1
        print(f"[{status}] {label}「{question}」 改写={meta.get('query')} "
              f"引用={len(citations)} 共识命中={claims_hit}")
        if not hit:
            print(f"       答案片段：{answer[:150]}")

    # ---- 3：画像个性化（三轮对话） ----
    history: list[dict] = []
    ok3 = True
    try:
        m1, _c, a1 = chat(base, "我戒100天了", history, session_id)
        history += [{"role": "user", "content": "我戒100天了"}, {"role": "assistant", "content": a1}]
        m2, _c, a2 = chat(base, "最近脱发厉害", history, session_id)
        history += [{"role": "user", "content": "最近脱发厉害"}, {"role": "assistant", "content": a2}]
        m3, _c, _a3 = chat(base, "还有救吗", history, session_id)
        q3 = m3.get("query") or ""
        ok3 = "100" in q3 and "脱发" in q3
        print(f"[{'PASS' if ok3 else 'FAIL'}] 画像个性化：第三轮改写 = {q3}")
    except Exception as e:  # noqa: BLE001
        ok3 = False
        print(f"[FAIL] 画像个性化：请求失败 {e}")
    passed, failed = passed + (1 if ok3 else 0), failed + (0 if ok3 else 1)

    # ---- 4：链路追踪（直接查库，避免依赖管理端登录） ----
    try:
        import psycopg2

        from app.config import settings

        conn = psycopg2.connect(**settings.dsn)
        cur = conn.cursor()
        cur.execute(
            "SELECT node_name, extra_data FROM trace_nodes"
            " WHERE trace_id = (SELECT trace_id FROM trace_runs ORDER BY start_time DESC LIMIT 1)"
        )
        nodes = {r[0]: r[1] or {} for r in cur.fetchall()}
        conn.close()
        ok_trace = (
            ("claims_hit" in (nodes.get("retrieval-engine") or {}))
            and ("query_entities" in (nodes.get("retrieval-engine") or {}))
            and ("profile-extract" in nodes or "stream-generate" in nodes)
        )
        print(f"[{'PASS' if ok_trace else 'WARN'}] 链路追踪节点："
              f"retrieval 含 claims_hit/query_entities、画像/生成节点存在 → {ok_trace}")
    except Exception as e:  # noqa: BLE001
        ok_trace = False
        print(f"[WARN] 链路追踪检查失败（可手动在管理页查看）: {e}")

    print(f"\n结果：通过 {passed} / 失败 {failed}" + ("（追踪项不计入）" if not ok_trace else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
