"""FastAPI 入口：阅读服务 + 问答服务（SSE 流式） + 前端静态托管（PostgreSQL 存储）。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.errors import UniqueViolation

from .config import BACKEND_DIR, settings
from .db import close_pool, conn_ctx, init_pool
from .auth import (
    admin_required, current_user, hash_password, issue_token, optional_user,
    revoke_token, validate_credentials, verify_password,
)
from .generator import Generator
from .intent import DEFAULT_RULES, detect_intent, refresh_intent_rules
from .models import (
    BookOut, ChatRequest, ChatResponse, CitationOut, DocOut, FeedbackIn,
    HealthOut, IntentRuleIn, IntentRuleOut, LoginIn, ParagraphOut, RegisterIn,
    SearchRequest, SearchResponse, SyncSessionIn, TocOut, TokenOut, UserOut,
)
from .retriever import Retriever

_state: dict[str, Any] = {}

# 用户/会话/反馈表（幂等创建，不动知识库已有表）
_USER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT,
    title TEXT NOT NULL DEFAULT '新对话',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    msg_index INT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    thinking TEXT,
    query TEXT,
    citations JSONB NOT NULL DEFAULT '[]',
    thinking_seconds INT,
    PRIMARY KEY (session_id, msg_index)
);
CREATE TABLE IF NOT EXISTS message_feedback (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    msg_index INT NOT NULL,
    subject TEXT NOT NULL,
    rating SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, msg_index, subject)
);
-- 老库迁移：user_id 改可空（游客会话按 device_id 归属），补 device_id 列
ALTER TABLE chat_sessions ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS device_id TEXT;
CREATE INDEX IF NOT EXISTS idx_sessions_user ON chat_sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON chat_sessions(device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_feedback_subject ON message_feedback(subject);
CREATE TABLE IF NOT EXISTS intent_rules (
    id SERIAL PRIMARY KEY,
    pattern TEXT NOT NULL UNIQUE,
    builtin BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(_USER_SCHEMA_SQL)
            # ---- 种子数据：内置管理员（不可删除）+ 意图规则 ----
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')"
                " ON CONFLICT (username) DO NOTHING",
                ("admin", hash_password(settings.admin_password)),
            )
            for pat in DEFAULT_RULES:
                cur.execute(
                    "INSERT INTO intent_rules (pattern, builtin) VALUES (%s, TRUE)"
                    " ON CONFLICT (pattern) DO NOTHING", (pat,),
                )
        _state["retriever"] = Retriever(conn)
    except Exception as e:  # noqa: BLE001
        close_pool()
        raise RuntimeError(
            f"知识库不可用（{e}）。请先运行 python scripts/build_kb.py 构建知识库。"
        ) from e
    _state["generator"] = Generator()
    refresh_intent_rules()
    print(f"[startup] 知识库已加载（PostgreSQL {settings.pg_host}:{settings.pg_port}/{settings.pg_db}）："
          f"chunks={len(_state['retriever'].chunk_meta)}, "
          f"vector={'on' if _state['retriever'].vector_ready else 'off'}, "
          f"llm={'on' if _state['generator'].enabled else 'off'}")
    yield
    close_pool()


app = FastAPI(title="戒色知识库 RAG 问答", lifespan=lifespan)


# ---------------- 阅读服务 ----------------

@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    with conn_ctx() as conn, conn.cursor() as cur:
        counts = {}
        for t in ("books", "docs", "paras", "chunks"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            counts[t] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM chunks WHERE dup_of IS NULL")
        indexed = cur.fetchone()[0]
    return HealthOut(
        books=counts["books"], docs=counts["docs"], paragraphs=counts["paras"],
        chunks=counts["chunks"], chunks_indexed=indexed,
        llm=_state["generator"].enabled,
        vector=_state["retriever"].vector_ready,
    )


@app.get("/api/books", response_model=list[BookOut])
def list_books() -> list[BookOut]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, title, author, doc_count, char_count FROM books ORDER BY id")
        return [BookOut(**dict(r)) for r in cur.fetchall()]


@app.get("/api/books/{book_id}/toc", response_model=list[TocOut])
def book_toc(book_id: str) -> list[TocOut]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, doc_id, para_id, title, level, seq FROM toc WHERE book_id=%s ORDER BY seq",
            (book_id,),
        )
        rows = cur.fetchall()
    if not rows:
        raise HTTPException(404, "book not found")
    return [TocOut(**dict(r)) for r in rows]


@app.get("/api/docs/{doc_id}", response_model=DocOut)
def get_doc(doc_id: str) -> DocOut:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, book_id, seq, title, author FROM docs WHERE id=%s", (doc_id,)
        )
        doc = cur.fetchone()
        if not doc:
            raise HTTPException(404, "doc not found")
        cur.execute(
            "SELECT id FROM docs WHERE book_id=%s AND seq=%s",
            (doc["book_id"], doc["seq"] - 1),
        )
        prev_row = cur.fetchone()
        cur.execute(
            "SELECT id FROM docs WHERE book_id=%s AND seq=%s",
            (doc["book_id"], doc["seq"] + 1),
        )
        next_row = cur.fetchone()
        cur.execute(
            "SELECT id, seq, kind, text FROM paras WHERE doc_id=%s ORDER BY seq", (doc_id,)
        )
        paras = cur.fetchall()
    return DocOut(
        id=doc["id"], book_id=doc["book_id"], title=doc["title"], author=doc["author"],
        prev_id=prev_row["id"] if prev_row else None,
        next_id=next_row["id"] if next_row else None,
        paragraphs=[ParagraphOut(**dict(p)) for p in paras],
    )


# ---------------- 问答服务 ----------------

@app.post("/api/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    if not req.query.strip():
        raise HTTPException(400, "query is empty")
    results = _state["retriever"].retrieve(req.query, k=req.k, book_id=req.book_id)
    return SearchResponse(
        query=req.query,
        results=[CitationOut(**c.to_dict()) for c in results],
    )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """SSE 流式问答：meta → citations → thinking* → answer+ → done。"""
    if not req.message.strip():
        raise HTTPException(400, "message is empty")
    gen: Generator = _state["generator"]
    history = [{"role": m.role, "content": m.content} for m in req.history if m.role in ("user", "assistant")]

    async def event_stream():
        query = req.message
        if gen.enabled and history:
            query = await gen.rewrite_query(req.message, history)
        citations = _state["retriever"].retrieve(
            query, smart_search=req.smart_search, book_id=req.book_id)
        yield _sse("meta", {"query": query, "mode": "llm" if gen.enabled else "extractive", "intent": detect_intent(query)})
        yield _sse("citations", [c.to_dict() for c in citations])

        if not citations:
            # 阈值过滤后无相关内容：直接告知，不调 LLM 硬编
            yield _sse("answer", {"delta": Generator._extractive(query, citations)})
            yield _sse("done", {})
            return

        async for kind, delta in gen.stream_generate(query, citations, deep_thinking=req.deep_thinking):
            if delta:
                yield _sse(kind, {"delta": delta})
        yield _sse("done", {})

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- 账号认证 ----------------

def _claim_guest_sessions(cur, user_id: int, device_id: str | None) -> None:
    """登录/注册时认领该设备产生的游客会话，归入账号。"""
    if not device_id:
        return
    cur.execute(
        "UPDATE chat_sessions SET user_id=%s WHERE device_id=%s AND user_id IS NULL",
        (user_id, device_id),
    )


@app.post("/api/auth/register", response_model=TokenOut)
def register(req: RegisterIn) -> TokenOut:
    validate_credentials(req.username, req.password)
    if req.email and "@" not in req.email:
        raise HTTPException(400, "邮箱格式不正确（选填）")
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, email) VALUES (%s, %s, %s) RETURNING id, username, email",
                (req.username, hash_password(req.password), req.email),
            )
            user_id = cur.fetchone()["id"]
            _claim_guest_sessions(cur, user_id, req.device_id)
    except UniqueViolation:
        raise HTTPException(409, "用户名已被注册") from None
    token = issue_token(user_id)
    return TokenOut(token=token, user=UserOut(id=user_id, username=req.username, email=req.email))


@app.post("/api/auth/login", response_model=TokenOut)
def login(req: LoginIn) -> TokenOut:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, email, role, password_hash FROM users WHERE username=%s",
            (req.username,),
        )
        row = cur.fetchone()
    if row is None or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    with conn_ctx() as conn, conn.cursor() as cur:
        _claim_guest_sessions(cur, row["id"], req.device_id)
    token = issue_token(row["id"])
    return TokenOut(token=token, user=UserOut(**{k: row[k] for k in ("id", "username", "email")}))


@app.post("/api/auth/logout")
def logout(user: dict = Depends(current_user), authorization: str = Header(default=None)) -> dict:
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token:
        revoke_token(token)
    return {"ok": True}


@app.get("/api/auth/me", response_model=UserOut)
def me(user: dict = Depends(current_user)) -> UserOut:
    return UserOut(**user)


# ---------------- 会话云同步（登录用户按账号、游客按设备标识） ----------------

def _sync_subject(user: dict | None, device_id: str | None) -> tuple[int | None, str | None]:
    """返回 (user_id, device_id)：登录用户走账号，游客走设备，二者都没有则 401。"""
    if user:
        return user["id"], None
    if device_id:
        return None, device_id
    raise HTTPException(401, "缺少登录凭证或设备标识")


@app.get("/api/sessions")
def list_sessions(
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> list[dict]:
    uid, did = _sync_subject(user, device_id)
    where, args = (("user_id=%s", (uid,)) if uid
                   else ("device_id=%s AND user_id IS NULL", (did,)))
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, title, created_at FROM chat_sessions WHERE {where} ORDER BY created_at DESC",
            args,
        )
        sessions = [dict(r) for r in cur.fetchall()]
        for s in sessions:
            cur.execute(
                "SELECT msg_index, role, content, thinking, query, citations, thinking_seconds"
                " FROM chat_messages WHERE session_id=%s ORDER BY msg_index",
                (s["id"],),
            )
            s["messages"] = [
                {
                    "role": r["role"], "content": r["content"], "thinking": r["thinking"],
                    "query": r["query"], "citations": r["citations"],
                    "thinkingSeconds": r["thinking_seconds"],
                }
                for r in cur.fetchall()
            ]
    return sessions


@app.put("/api/sessions/{sid}")
def save_session(
    sid: str,
    req: SyncSessionIn,
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> dict:
    if req.id != sid:
        raise HTTPException(400, "会话 id 不一致")
    uid, did = _sync_subject(user, device_id)
    now_ms = int(datetime.now().timestamp() * 1000)
    with conn_ctx(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, device_id FROM chat_sessions WHERE id=%s FOR UPDATE", (sid,))
        row = cur.fetchone()
        if row is not None:
            owned = (uid is not None and row["user_id"] == uid) or (
                uid is None and row["user_id"] is None and did is not None
                and row["device_id"] == did
            )
            if not owned:
                raise HTTPException(403, "无权修改该会话")
        cur.execute(
            "INSERT INTO chat_sessions (id, user_id, device_id, title, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, updated_at=EXCLUDED.updated_at",
            (sid, uid, did, req.title, req.created_at, now_ms),
        )
        cur.execute("DELETE FROM chat_messages WHERE session_id=%s", (sid,))
        for i, m in enumerate(req.messages):
            if m.role not in ("user", "assistant"):
                raise HTTPException(400, f"非法消息角色: {m.role}")
            cur.execute(
                "INSERT INTO chat_messages (session_id, msg_index, role, content, thinking,"
                " query, citations, thinking_seconds) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (sid, i, m.role, m.content, m.thinking, m.query,
                 json.dumps(m.citations, ensure_ascii=False), m.thinking_seconds),
            )
        conn.commit()
    return {"ok": True, "updated_at": now_ms}


@app.delete("/api/sessions/{sid}")
def delete_session(
    sid: str,
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> dict:
    uid, did = _sync_subject(user, device_id)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, device_id FROM chat_sessions WHERE id=%s", (sid,))
        row = cur.fetchone()
        if row is not None:
            owned = (uid is not None and row["user_id"] == uid) or (
                uid is None and row["user_id"] is None and did is not None
                and row["device_id"] == did
            )
            if not owned:
                raise HTTPException(403, "无权删除该会话")
            cur.execute("DELETE FROM chat_sessions WHERE id=%s", (sid,))
    return {"ok": True}


# ---------------- 点赞点踩（游客也可记录） ----------------

def _feedback_subject(user: dict | None, device_id: str | None) -> str:
    return f"u:{user['id']}" if user else f"g:{device_id or 'anon'}"


@app.post("/api/feedback")
def post_feedback(req: FeedbackIn, user: dict | None = Depends(optional_user)) -> dict:
    if req.rating not in (1, -1, 0):
        raise HTTPException(400, "rating 只能为 1 / -1 / 0")
    subject = _feedback_subject(user, req.device_id)
    with conn_ctx() as conn, conn.cursor() as cur:
        if req.rating == 0:
            cur.execute(
                "DELETE FROM message_feedback WHERE session_id=%s AND msg_index=%s AND subject=%s",
                (req.session_id, req.msg_index, subject),
            )
        else:
            cur.execute(
                "INSERT INTO message_feedback (session_id, msg_index, subject, rating)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (session_id, msg_index, subject) DO UPDATE SET rating=EXCLUDED.rating",
                (req.session_id, req.msg_index, subject, req.rating),
            )
    return {"rating": req.rating}


@app.get("/api/feedback")
def get_feedback(
    session_id: str,
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> dict:
    subject = _feedback_subject(user, device_id)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT msg_index, rating FROM message_feedback WHERE session_id=%s AND subject=%s",
            (session_id, subject),
        )
        rows = cur.fetchall()
    return {"ratings": {str(r["msg_index"]): r["rating"] for r in rows}}


# ---------------- 前端静态托管 ----------------


# ---------------- 管理后台（仅内置 admin 角色） ----------------

@app.get("/api/admin/users", response_model=list[UserOut])
def admin_list_users(_: dict = Depends(admin_required)) -> list[UserOut]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, email, role FROM users ORDER BY id")
        return [UserOut(**dict(r)) for r in cur.fetchall()]


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, admin: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id=%s", (uid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        if row["role"] == "admin":
            raise HTTPException(400, "内置管理员不可删除")
        if uid == admin["id"]:
            raise HTTPException(400, "不能删除自己")
        cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    return {"ok": True}


@app.get("/api/admin/intent-rules", response_model=list[IntentRuleOut])
def admin_list_rules(_: dict = Depends(admin_required)) -> list[IntentRuleOut]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, pattern, builtin, enabled, created_at FROM intent_rules ORDER BY id")
        return [IntentRuleOut(**{**dict(r), "created_at": str(r["created_at"])}) for r in cur.fetchall()]


@app.post("/api/admin/intent-rules", response_model=IntentRuleOut)
def admin_add_rule(req: IntentRuleIn, _: dict = Depends(admin_required)) -> IntentRuleOut:
    import re as _re
    try:
        _re.compile(req.pattern)
    except _re.error:
        raise HTTPException(400, "无效的正则表达式")
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO intent_rules (pattern) VALUES (%s)"
                " RETURNING id, pattern, builtin, enabled, created_at",
                (req.pattern.strip(),),
            )
            r = cur.fetchone()
    except Exception:
        raise HTTPException(400, "规则已存在或写入失败")
    refresh_intent_rules()
    return IntentRuleOut(**{**dict(r), "created_at": str(r["created_at"])})


@app.patch("/api/admin/intent-rules/{rid}", response_model=IntentRuleOut)
def admin_toggle_rule(rid: int, _: dict = Depends(admin_required)) -> IntentRuleOut:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE intent_rules SET enabled = NOT enabled WHERE id=%s"
            " RETURNING id, pattern, builtin, enabled, created_at", (rid,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "规则不存在")
    refresh_intent_rules()
    return IntentRuleOut(**{**dict(r), "created_at": str(r["created_at"])})


@app.delete("/api/admin/intent-rules/{rid}")
def admin_delete_rule(rid: int, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT builtin FROM intent_rules WHERE id=%s", (rid,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(404, "规则不存在")
        if r["builtin"]:
            raise HTTPException(400, "内置规则不可删除，可停用")
        cur.execute("DELETE FROM intent_rules WHERE id=%s", (rid,))
    refresh_intent_rules()
    return {"ok": True}


_static_dir = BACKEND_DIR / "static"
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
else:
    @app.get("/")
    def root_hint() -> dict:
        return {
            "message": "前端未构建。请执行 cd frontend && npm install && npm run build，"
                       "或使用 npm run dev 启动开发服务器。API 文档见 /docs"
        }
