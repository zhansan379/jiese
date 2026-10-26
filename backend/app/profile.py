"""对话画像槽位（实现计划功能三：docs/实现计划-共识层-实体-画像.md）。

- profile 挂在 chat_sessions（JSONB）：{"days_quit": "100天", "symptoms": ["脱发"], "identity": "学生党"}
  游客按 device、登录按账号，与现有会话云同步一致。
- 槽位抽取：每轮一次轻量 LLM 调用（temp 0 / max_tokens 100），与检索并发执行
  （asyncio.gather，消除首包延迟）；结果落库后从下一轮起影响查询改写，本轮立即影响生成。
- 合并规则：days_quit / identity 取最新非空；symptoms 取并集（上限 10 个）。
- 与 memory.py 的边界：memory = 对话内容摘要（聊了什么），profile = 结构化槽位（用户是谁），
  两者独立注入互不覆盖；注入顺序 memory 摘要 → profile 槽位 → 摘录。
"""
from __future__ import annotations

import json
import re
import time

from fastapi.concurrency import run_in_threadpool

from .db import conn_ctx

MAX_SYMPTOMS = 10

_EXTRACT_SYSTEM = (
    "从用户最新发言中抽取对话画像槽位，未提及的字段输出 null（症状未提及输出空数组 []）。"
    "days_quit 是明确的戒色时长（如\"100天\"），symptoms_new 是本次新提及的身体/心理症状"
    "（如脱发、频遗、社恐、失眠），identity 是身份（如学生党、上班族、高中生）。"
    "只输出一行 JSON，不要输出任何其他内容："
    '{"days_quit": "100天"|null, "symptoms_new": ["脱发"]|[], "identity": "学生党"|null}'
)

_JSON_RE = re.compile(r"\{.*\}", re.S)


def load_profile(session_id: str) -> dict:
    """读取会话画像（无会话/无画像返回空 dict）。线程池中调用。"""
    if not session_id:
        return {}
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT profile FROM chat_sessions WHERE id=%s", (session_id,))
        row = cur.fetchone()
    if not row or not row["profile"]:
        return {}
    p = row["profile"]
    if isinstance(p, dict):
        return p
    try:
        return json.loads(p)
    except (TypeError, ValueError):
        return {}


def save_profile(session_id: str, profile: dict) -> None:
    """画像落库（线程池中调用）。

    首条消息时前端会话行可能尚未同步创建（600ms 防抖）：UPDATE 未命中则补建占位行，
    user_id/device_id 留空，由随后的会话同步认领（见 main._save_session_db 的 COALESCE）。
    """
    if not session_id or not profile:
        return
    now_ms = int(time.time() * 1000)
    payload = json.dumps(profile, ensure_ascii=False)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("UPDATE chat_sessions SET profile=%s WHERE id=%s", (payload, session_id))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO chat_sessions (id, title, created_at, updated_at, profile)"
                " VALUES (%s, '新对话', %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (session_id, now_ms, now_ms, payload),
            )


async def extract_slots(generator, message: str) -> dict | None:
    """从用户最新发言抽取画像槽位（轻量 LLM 调用）。失败/解析不出返回 None。"""
    try:
        raw = await generator.complete(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": message},
            ],
            temperature=0.0,
            max_tokens=100,
        )
        m = _JSON_RE.search(raw or "")
        if not m:
            return None
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return None
        return {
            "days_quit": data.get("days_quit"),
            "symptoms_new": data.get("symptoms_new"),
            "identity": data.get("identity"),
        }
    except Exception as e:  # noqa: BLE001 画像抽取失败不影响主链路
        print(f"[profile] 槽位抽取失败: {e}")
        return None


def merge_profile(profile: dict, extracted: dict) -> tuple[dict, dict]:
    """合并抽取结果到已有画像。返回 (合并后的完整画像, 本轮增量)。"""
    new = dict(profile)
    delta: dict = {}
    days = (extracted.get("days_quit") or "").strip() if isinstance(extracted.get("days_quit"), str) else ""
    if days:
        new["days_quit"] = days
        delta["days_quit"] = days
    identity = (extracted.get("identity") or "").strip() if isinstance(extracted.get("identity"), str) else ""
    if identity:
        new["identity"] = identity
        delta["identity"] = identity
    symptoms = extracted.get("symptoms_new")
    if isinstance(symptoms, str):
        symptoms = [symptoms]
    if isinstance(symptoms, list):
        cleaned = [s.strip() for s in symptoms if isinstance(s, str) and s.strip()]
        existing = list(new.get("symptoms") or [])
        added = [s for s in cleaned if s not in existing]
        if added:
            new["symptoms"] = (existing + added)[:MAX_SYMPTOMS]
            delta["symptoms_new"] = added
    return new, delta


async def extract_and_save(generator, session_id: str, message: str, profile: dict) -> dict | None:
    """一轮的画像更新：抽取 → 合并 → 落库（有增量才写）。

    返回 {"profile": 合并后完整画像, "delta": 本轮增量}；无增量/失败返回 None。
    与检索并发执行：调用方在检索完成后 await 本协程的结果。
    """
    extracted = await extract_slots(generator, message)
    if not extracted:
        return None
    new_profile, delta = merge_profile(profile, extracted)
    if not delta:
        return {"profile": new_profile, "delta": {}}
    await run_in_threadpool(save_profile, session_id, new_profile)
    return {"profile": new_profile, "delta": delta}


def profile_prompt(profile: dict) -> str:
    """画像 → 注入 prompt 的一行摘要（生成与改写共用）。空画像返回空串。"""
    parts = []
    if profile.get("days_quit"):
        parts.append(f"戒色时长 {profile['days_quit']}")
    if profile.get("symptoms"):
        parts.append(f"曾提及症状：{'、'.join(profile['symptoms'])}")
    if profile.get("identity"):
        parts.append(f"身份：{profile['identity']}")
    return "；".join(parts)
