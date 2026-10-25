"""会话记忆（参考 nageoffer/ragent 的「最近 N 轮原文 + 持久化摘要」方案）。

- load：读取会话的持久化摘要，注入 prompt 时作为独立的 system 背景（wrapper 包裹）。
- 压缩：会话保存（PUT /api/sessions）后异步触发——user 消息数达到阈值时，
  把「旧摘要 + 已滑出保留窗口的消息」交给 LLM 合并去重为一条短摘要，持久化到
  conversation_summaries，游标 last_msg_index 记录覆盖进度；只有当摘要边界
  重新逼近保留窗口时才再次压缩，避免每轮都调 LLM。
- 并发：ragent 用 Redis 分布式锁，本项目无 Redis，改用 PostgreSQL advisory lock
  只锁住「规划」阶段（快速读写）；写入用单调游标防护（last_msg_index 不回退），
  即使两个压缩任务并发调用 LLM，后写者游标更靠前才生效，最坏浪费一次摘要调用。
"""
from __future__ import annotations

import asyncio
import re
import time

from fastapi.concurrency import run_in_threadpool

from .config import settings
from .db import conn_ctx

# assistant 回答中的 [n] 引用标记：注入历史/摘要前剥掉，避免与摘录编号混淆
# （ragent 对应 CitationMarkup.strip）
_CITATION_RE = re.compile(r"\s*\[\d+\]")


def strip_citations(text: str) -> str:
    return _CITATION_RE.sub("", text or "")


def load_summary(session_id: str) -> str | None:
    """读取会话当前摘要（无摘要/会话不存在返回 None）。线程池中调用。"""
    if not session_id:
        return None
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM conversation_summaries WHERE session_id=%s", (session_id,)
        )
        row = cur.fetchone()
    return row["content"] if row else None


def build_memory_messages(
    summary: str | None, history: list[dict], context_turns: int | None = None
) -> list[dict]:
    """构造注入生成 prompt 的记忆消息：摘要 system + 最近 N 轮原文。

    ragent 的 attachSummary 把摘要放最前，最近轮次随后；assistant 消息剥引用标记。
    """
    messages: list[dict] = []
    if summary and summary.strip():
        messages.append({
            "role": "system",
            "content": f"以下是本轮之前对话的背景摘要，仅用于理解用户意图与指代，不要据此直接回答：\n{summary.strip()}",
        })
    turns = (context_turns if context_turns is not None else settings.memory_context_turns) * 2
    for m in history[-turns:]:
        content = strip_citations(m.get("content", ""))
        if m.get("role") in ("user", "assistant") and content.strip():
            messages.append({"role": m["role"], "content": content})
    return messages


def _plan_compress(session_id: str) -> dict | None:
    """计算压缩计划：窗口、水位、待摘要消息。advisory lock 内完成（线程池中调用）。"""
    keep_turns = settings.memory_history_turns
    start_turns = settings.memory_summary_start_turns
    if keep_turns <= 0 or start_turns <= 0:
        return None
    lock_key = f"jiese:memory:summary:{session_id}"
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,))
        if not cur.fetchone()[0]:
            return None
        try:
            # 触发水位：user 消息数（ragent: countUserMessages >= summaryStartTurns）
            cur.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id=%s AND role='user'",
                (session_id,),
            )
            if cur.fetchone()[0] < start_turns:
                return None

            # 保留窗口：最近 keep_turns 条 user 消息（一条 user 代表一轮的起点）
            cur.execute(
                "SELECT msg_index FROM chat_messages WHERE session_id=%s AND role='user'"
                " ORDER BY msg_index DESC LIMIT %s",
                (session_id, keep_turns),
            )
            window = [r["msg_index"] for r in cur.fetchall()]  # 倒序
            if not window:
                return None
            history_start = window[-1]  # 窗口内最早 user 消息

            cur.execute(
                "SELECT content, last_msg_index FROM conversation_summaries WHERE session_id=%s",
                (session_id,),
            )
            summary_row = cur.fetchone()
            existing = (summary_row["content"] or "").strip() if summary_row else ""
            after = summary_row["last_msg_index"] if summary_row else 0
            # 摘要已覆盖进保留窗口 → 无可压缩内容
            if summary_row and after >= history_start:
                return None

            # 截断点：窗口中点的 user 消息（ragent: get((size-1)/2)），
            # 摘要覆盖约一半窗口；紧随其后的 assistant 回复一并纳入，按完整轮切分
            cutoff = window[(len(window) - 1) // 2]
            last_idx = cutoff
            cur.execute(
                "SELECT msg_index, role FROM chat_messages WHERE session_id=%s AND msg_index=%s",
                (session_id, cutoff + 1),
            )
            nxt = cur.fetchone()
            if nxt and nxt["role"] == "assistant":
                last_idx = cutoff + 1
            if after >= last_idx:
                return None

            cur.execute(
                "SELECT msg_index, role, content FROM chat_messages"
                " WHERE session_id=%s AND msg_index > %s AND msg_index <= %s ORDER BY msg_index",
                (session_id, after, last_idx),
            )
            rows = cur.fetchall()
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))

    messages = [
        {"role": r["role"], "content": r["content"] or ""}
        for r in rows if r["role"] in ("user", "assistant") and (r["content"] or "").strip()
    ]
    if not messages:
        return None
    # 超长保护：丢弃最早的消息（该段不再回补摘要，仅影响超长会话的早期细节）
    total = 0
    for i in range(len(messages) - 1, -1, -1):
        total += len(messages[i]["content"])
        if total > 30000:
            messages = messages[i + 1:]
            break
    if not messages:
        return None
    return {"messages": messages, "existing": existing, "last_msg_index": last_idx}


async def _summarize(generator, plan: dict) -> str:
    max_chars = settings.memory_summary_max_chars
    msgs: list[dict] = [{
        "role": "system",
        "content": (
            f"你是对话摘要压缩器。把对话内容与已有历史摘要合并去重，输出一段不超过 "
            f"{max_chars} 字的简体中文摘要。保留：用户的核心问题与关注点、已确认的结论"
            f"与建议方法、用户的个人情况（如状态、目标、困扰）。不要新增对话之外的事实，"
            f"不要评价。只输出摘要本身，仅一行。"
        ),
    }]
    if plan["existing"]:
        msgs.append({
            "role": "assistant",
            "content": "已有历史摘要（仅用于合并去重，不得作为事实新增来源；"
                       f"若与本轮对话冲突，以本轮对话为准）：\n{plan['existing']}",
        })
    msgs.extend({"role": m["role"], "content": m["content"]} for m in plan["messages"])
    msgs.append({
        "role": "user",
        "content": f"合并以上对话与历史摘要，去重后输出更新摘要。要求：严格≤{max_chars}字符；仅一行。",
    })
    return (await generator.complete(msgs, temperature=0.3, max_tokens=512)).strip()


def _save_summary(session_id: str, content: str, last_msg_index: int) -> None:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_summaries (session_id, content, last_msg_index, updated_at)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (session_id) DO UPDATE SET"
            "   content=EXCLUDED.content, last_msg_index=EXCLUDED.last_msg_index,"
            "   updated_at=EXCLUDED.updated_at"
            " WHERE conversation_summaries.last_msg_index <= EXCLUDED.last_msg_index",
            (session_id, content, last_msg_index, int(time.time() * 1000)),
        )


async def compress_if_needed(session_id: str, generator) -> None:
    """保存会话后调用的后台任务入口（对齐 ragent 的 append → compressIfNeeded）。"""
    if not settings.memory_summary_enabled or not generator.enabled or not session_id:
        return
    try:
        plan = await run_in_threadpool(_plan_compress, session_id)
        if not plan:
            return
        summary = await _summarize(generator, plan)
        if summary:
            await run_in_threadpool(_save_summary, session_id, summary, plan["last_msg_index"])
            print(f"[memory] 会话摘要已更新 - session: {session_id}, "
                  f"覆盖至 msg_index={plan['last_msg_index']}, 摘要 {len(summary)} 字")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 会话摘要压缩失败 - session: {session_id}: {e}")
