"""FastAPI 入口：阅读服务 + 问答服务（SSE 流式） + 前端静态托管（PostgreSQL 存储）。"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.errors import UniqueViolation

from .config import BACKEND_DIR, settings
from .claims import build_consensus_signal
from .db import close_pool, conn_ctx, init_pool
from .auth import (
    admin_required, current_user, hash_password, issue_token, optional_user,
    revoke_token, validate_credentials, verify_password,
)
from .entities import collect_method_hints, match_entities, refresh as refresh_entities
from .generator import Generator
from .intent import classify, refresh_intent_enabled, refresh_intent_tree
from .memory import build_memory_messages, compress_if_needed, load_summary
from .profile import extract_and_save, load_profile, profile_prompt
from .term_mapping import normalize_with_hits
from .term_mapping import refresh as refresh_term_mappings
from .tracing import TraceRecorder, trace_node
from .models import (
    BookOut, ChatRequest, ChatResponse, CitationOut, DocOut, FeedbackIn,
    HealthOut, LoginIn, ParagraphOut, RegisterIn,
    SearchRequest, SearchResponse, ShareCreateIn, SyncSessionIn, TocOut, TokenOut, UserOut,
)
from .retriever import Retriever

_state: dict[str, Any] = {}
_bg_tasks: set[asyncio.Task] = set()   # 持有后台任务强引用，防止被 GC 中途回收

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
-- 分享链接：会话快照（jsonb），公开只读；作者凭登录态或设备标识删除
CREATE TABLE IF NOT EXISTS share_links (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    user_id INT REFERENCES users(id) ON DELETE CASCADE,
    device_id TEXT,
    view_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_share_links_device ON share_links(device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_share_links_user ON share_links(user_id) WHERE user_id IS NOT NULL;
-- 老库迁移：user_id 改可空（游客会话按 device_id 归属），补 device_id 列
ALTER TABLE chat_sessions ALTER COLUMN user_id DROP NOT NULL;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS device_id TEXT;
-- 对话画像槽位（功能三）：结构化用户画像挂会话，与云同步一致（游客按 device、登录按账号）
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS profile JSONB DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_sessions_user ON chat_sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_device ON chat_sessions(device_id) WHERE device_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_feedback_subject ON message_feedback(subject);
-- 会话记忆：持久化摘要（参考 ragent「最近 N 轮原文 + 摘要」），last_msg_index 为覆盖游标
CREATE TABLE IF NOT EXISTS conversation_summaries (
    session_id TEXT PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    last_msg_index INT NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intent_nodes (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_code TEXT,
    kind TEXT NOT NULL DEFAULT 'kb',
    action JSONB NOT NULL DEFAULT '{}',
    description TEXT DEFAULT '',
    examples TEXT DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    builtin BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 查询词映射（参考 ragent t_query_term_mapping：口语/缩写 → 标准术语归一化）
CREATE TABLE IF NOT EXISTS query_term_mappings (
    id SERIAL PRIMARY KEY,
    domain TEXT NOT NULL DEFAULT '',
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    match_type SMALLINT NOT NULL DEFAULT 1,   -- 1 精确 2 前缀 3 正则 4 整词
    priority INT NOT NULL DEFAULT 100,        -- 数值越小越先应用
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    remark TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_term, match_type)
);
-- 链路追踪（参考 ragent t_rag_trace_run / t_rag_trace_node）
CREATE TABLE IF NOT EXISTS trace_runs (
    trace_id TEXT PRIMARY KEY,
    trace_name TEXT NOT NULL DEFAULT '',
    entry_method TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING / SUCCESS / ERROR / CANCELLED
    error_message TEXT,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    duration_ms BIGINT,
    extra_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trace_runs_start ON trace_runs(start_time DESC);
CREATE INDEX IF NOT EXISTS idx_trace_runs_status ON trace_runs(status);
CREATE TABLE IF NOT EXISTS trace_nodes (
    id SERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    parent_node_id TEXT,
    depth INT NOT NULL DEFAULT 0,
    node_type TEXT NOT NULL DEFAULT 'METHOD', -- REWRITE / INTENT / RETRIEVE / LLM / USER_TTFT
    node_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    error_message TEXT,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    duration_ms BIGINT,
    extra_data JSONB NOT NULL DEFAULT '{}',
    UNIQUE (trace_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_trace_nodes_trace ON trace_nodes(trace_id, start_time);
-- 实体标注与受限关系（功能二）：术语表 + 关系边（方法—对治→问题、因素—导致→症状）
CREATE TABLE IF NOT EXISTS terms (
    term TEXT PRIMARY KEY,
    type TEXT NOT NULL,              -- 症状 | 方法 | 因素 | 概念
    aliases JSONB NOT NULL DEFAULT '[]',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS term_edges (
    id SERIAL PRIMARY KEY,
    src_term TEXT NOT NULL,          -- 方法/因素
    rel TEXT NOT NULL,               -- 对治 | 导致
    dst_term TEXT NOT NULL,          -- 问题/症状
    evidence JSONB NOT NULL DEFAULT '[]',   -- [chunk_id,...]；运行时仅用 ≥2 条的边
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_term, rel, dst_term)
);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS entities JSONB NOT NULL DEFAULT '{}';
-- Claim 共识层（功能一）：带支持度的经验点 + 证据关联 + 抽取缓存（断点续跑）
CREATE TABLE IF NOT EXISTS claims (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,                  -- 规范表述（簇内最完整的一条）
    centroid vector(1024),               -- 簇心向量（bge-m3）
    support_authors INT NOT NULL,        -- 去重作者数（共识度）
    support_chunks INT NOT NULL,         -- 支撑 chunk 数
    authors JSONB NOT NULL DEFAULT '[]', -- ["飞翔","禅行僧",...]
    books JSONB NOT NULL DEFAULT '{}',   -- {"jwly": 12, "jzl": 8}
    topic TEXT NOT NULL DEFAULT '其他',   -- 第二级分类（app/topics.py）
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE claims ADD COLUMN IF NOT EXISTS topic TEXT NOT NULL DEFAULT '其他';
CREATE INDEX IF NOT EXISTS idx_claims_topic ON claims(topic);
CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id INT REFERENCES claims(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE CASCADE,
    PRIMARY KEY (claim_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_chunk ON claim_evidence(chunk_id);
CREATE TABLE IF NOT EXISTS claim_extractions (
    chunk_id TEXT PRIMARY KEY,
    text_hash TEXT NOT NULL,
    claims JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 断言向量缓存：复跑时零嵌入 API、聚类结果可复现
CREATE TABLE IF NOT EXISTS claim_embeddings (
    text_hash TEXT PRIMARY KEY,
    vec vector(1024) NOT NULL,
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
            cur.execute("ALTER TABLE intent_nodes ADD COLUMN IF NOT EXISTS gen_prompt TEXT DEFAULT ''")
            cur.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, 'admin')"
                " ON CONFLICT (username) DO NOTHING",
                ("admin", hash_password(settings.admin_password)),
            )

            intent_seeds = [
                ("kb_jwly", "戒为良药（方法论）", "kb",
                 {"type": "kb", "book_id": "jwly"},
                 "飞翔《戒为良药》相关：戒色方法、断念实战、症状恢复、养生调理",
                 "断念口诀怎么用|频遗怎么控制|社恐如何恢复|熬夜久坐的危害",
                 "回答以飞翔的方法论体系为主，条理清晰，多引用章节与口诀原文，先讲原理再给可执行步骤"),
                ("kb_jzl", "戒者录（戒友经验）", "kb",
                 {"type": "kb", "book_id": "jzl"},
                 "《戒者录》戒友真实经历与感悟：恢复历程、心路变化、经验分享",
                 "有没有人戒过一年以上|戒色后生活变化|前辈们都怎么熬过来的",
                 "回答以戒友真实经历为主，语气亲切平和，多引用具体案例与天数里程碑，突出'有人挺过来了'的支撑感"),
                ("kb_all", "全部知识库", "kb",
                 {"type": "kb", "book_id": None},
                 "跨两本书的综合问题，或无法明确归入单本书的戒色相关问题",
                 "戒色最难的是什么|意淫来了怎么办",
                 "兼顾方法论与真实经历：先给体系化方法，再补充一两条戒友实践佐证"),
                ("chitchat", "闲聊与无关问题", "system",
                 {"type": "system",
                  "reply": "我是基于《戒为良药》与《戒者录》两本书的戒色知识助手，只回答与戒色、身心健康相关的问题。关于这方面的问题，随时可以问我。"},
                 "与戒色、身心健康无关的闲聊和提问：时事、技术、菜谱、行情等",
                 "红烧肉怎么做|最近股市怎么样|帮我写个Python脚本",
                 ""),
            ]
            for code, name, kind, action, desc, ex, gp in intent_seeds:
                cur.execute(
                    "INSERT INTO intent_nodes (code, name, kind, action, description, examples, gen_prompt, builtin, sort_order)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,%s) ON CONFLICT (code) DO UPDATE"
                    " SET gen_prompt = EXCLUDED.gen_prompt, description = EXCLUDED.description," 
                    " examples = EXCLUDED.examples, action = EXCLUDED.action",
                    (code, name, kind, json.dumps(action, ensure_ascii=False), desc, ex, gp,
                     {"kb_jwly": 10, "kb_jzl": 20, "kb_all": 30, "chitchat": 90}[code]),
                )
            cur.execute(
                "INSERT INTO intent_nodes (code, name, kind, action, sort_order)"
                " VALUES ('group_kb', '戒色知识', 'group', '{}', 1),"
                " ('group_sys', '系统交互', 'group', '{}', 80)"
                " ON CONFLICT (code) DO NOTHING"
            )
            cur.execute(
                "UPDATE intent_nodes SET parent_code = 'group_kb'"
                " WHERE code IN ('kb_jwly', 'kb_jzl', 'kb_all') AND parent_code IS NULL"
            )
            cur.execute(
                "UPDATE intent_nodes SET parent_code = 'group_sys'"
                " WHERE code = 'chitchat' AND parent_code IS NULL"
            )
            cur.execute("DROP TABLE IF EXISTS intent_rules")
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES ('intent_tree_enabled', '1')"
                " ON CONFLICT (key) DO NOTHING"
            )
            # ---- 查询词映射种子（内置示例，管理后台可改） ----
            term_seeds = [
                ("yy", "意淫", 4, "口语缩写归一化（内置示例）"),
                ("sy", "手淫", 4, "口语缩写归一化（内置示例）"),
                ("湿乎乎", "阴囊潮湿", 1, "口语描述归一化（实体加成链路）"),
                ("湿漉漉", "阴囊潮湿", 1, "口语描述归一化（实体加成链路）"),
            ]
            for src, tgt, mt, remark in term_seeds:
                cur.execute(
                    "INSERT INTO query_term_mappings (source_term, target_term, match_type, remark)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT (source_term, match_type) DO NOTHING",
                    (src, tgt, mt, remark),
                )
        _state["retriever"] = Retriever(conn)
    except Exception as e:  # noqa: BLE001
        close_pool()
        raise RuntimeError(
            f"知识库不可用（{e}）。请先运行 python scripts/build_kb.py 构建知识库。"
        ) from e
    _state["generator"] = Generator()
    refresh_intent_tree()
    from .intent import refresh_intent_enabled
    refresh_intent_enabled()
    refresh_entities()   # 实体匹配缓存预热（术语表缺失时静默降级为空字典）
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
    """SSE 流式问答：meta → citations → thinking* → answer+ → done。

    全链路追踪（参考 ragent StreamChatTraceRunner）：一个请求 = 一个 Run，
    归一化/改写/意图/检索/生成各阶段 = Node，首条内容推给前端时记 TTFT 节点。
    追踪关闭（settings.trace_enabled）时 rec 为 None，各节点 no-op。
    """
    if not req.message.strip():
        raise HTTPException(400, "message is empty")
    gen: Generator = _state["generator"]
    history = [{"role": m.role, "content": m.content} for m in req.history if m.role in ("user", "assistant")]
    # 提问者公开背景（如贴吧历史发帖画像）：只注入生成侧，检索查询保持原问题不污染
    user_context = (req.user_context or "").strip()[:800]
    # 会话记忆：持久化摘要（最近 N 轮之外的长程上下文，见 memory.py）
    summary = await run_in_threadpool(load_summary, req.session_id or "")
    # 对话画像槽位（profile.py）：与 memory 摘要独立注入、互不覆盖；本轮抽取的增量即时注入生成，
    # 落库后从下一轮起影响查询改写（画像挂会话，游客按 device、登录按账号）
    profile = await run_in_threadpool(load_profile, req.session_id or "")

    rec: TraceRecorder | None = None
    if settings.trace_enabled:
        rec = TraceRecorder(
            trace_name="rag-stream-chat",
            entry_method="chat#stream",
            session_id=req.session_id,
            extra={
                "question": req.message,
                "question_length": len(req.message),
                "deep_thinking": req.deep_thinking,
                "smart_search": req.smart_search,
                "book_id": req.book_id,
            },
        )
        await rec.start_run()

    first_content = {"sent": False}

    async def mark_first_content() -> None:
        """首条 answer/thinking 推给前端 → 记录用户感知首包（仅首次生效）。"""
        if rec and not first_content["sent"]:
            first_content["sent"] = True
            await rec.record_ttft()

    async def event_stream():
        # 查询词映射（ragent：改写前先做术语归一化，口语/缩写 → 标准术语）
        async with trace_node(rec, "term-mapping", "REWRITE") as n:
            normalized, map_hits = await run_in_threadpool(normalize_with_hits, req.message)
            n.set_extra(source=req.message, normalized=normalized, hits=map_hits)

        query = normalized
        if gen.enabled and history:
            async with trace_node(rec, "query-rewrite", "REWRITE") as n:
                query = await gen.rewrite_query(
                    normalized, history, summary=summary,
                    profile_summary=profile_prompt(profile),
                )
                n.set_extra(source=normalized, rewritten=query, profile_summary=profile_prompt(profile))

        # 意图识别（ragent 式两级：规则快通道 → LLM 叶子打分 → 兜底）
        async with trace_node(rec, "intent-resolve", "INTENT") as n:
            intent = await run_in_threadpool(classify, query)
            n.set_extra(code=intent.code, source=intent.source, score=round(intent.score, 3))
        yield _sse("meta", {
            "query": query, "mode": "llm" if gen.enabled else "extractive",
            "intent": {"code": intent.code, "source": intent.source},
            "has_summary": bool(summary),
        })

        # system 意图（闲聊/无关）：直接流式回复，不检索不生成；
        # 话术为空（配置缺失）时回退默认全库检索，保证用户总能得到回答
        if intent.action.get("type") == "system" and intent.action.get("reply"):
            reply = intent.action["reply"]
            await mark_first_content()
            for i in range(0, len(reply), 24):
                yield _sse("answer", {"delta": reply[i:i + 24]})
            yield _sse("done", {})
            return

        # 画像槽位抽取与检索并发执行（asyncio.gather 语义：抽取不阻塞本轮查询构造，
        # 消除首包延迟）；trace 节点记录本轮抽取的增量与合并后画像
        async def _extract_profile():
            async with trace_node(rec, "profile-extract", "LLM") as pn:
                result = await extract_and_save(gen, req.session_id, req.message, profile)
                if result:
                    pn.set_extra(delta=result["delta"], profile=result["profile"])
                else:
                    pn.set_extra(delta={})
                return result

        profile_task = None
        cur_profile = profile   # 本轮生成用的画像（抽取并发进行，完成后合并本轮增量）
        if gen.enabled and req.session_id:
            profile_task = asyncio.create_task(_extract_profile())
            _bg_tasks.add(profile_task)
            profile_task.add_done_callback(_bg_tasks.discard)

        async with trace_node(rec, "retrieval-engine", "RETRIEVE") as n:
            book_id = req.book_id or intent.action.get("book_id")
            citations = await run_in_threadpool(
                _state["retriever"].retrieve, query,
                smart_search=req.smart_search,
                book_id=book_id,
            )
            # 查询实体识别（功能二）：加权在 retrieve() 内完成，这里供 trace 与关系增强生成
            query_entities = match_entities(query)
            n.set_extra(query=query, book_id=book_id, count=len(citations),
                        query_entities=sorted(query_entities),
                        claims_hit=sum(len(c.claims) for c in citations))

        # 引用先发（不被画像抽取阻塞）：抽取与检索并发，仅在生成前汇聚结果
        yield _sse("citations", [c.to_dict() for c in citations])

        if profile_task is not None:
            try:
                prof = await profile_task
            except Exception as e:  # noqa: BLE001  画像失败不影响主链路
                print(f"[profile] 画像更新失败: {e}")
                prof = None
            if prof:
                cur_profile = prof["profile"]

        # 关系增强生成（功能二）：查询实体命中「对治」边 → 方法清单段（供回答骨架，[n] 可引用）
        entity_hint = collect_method_hints(query_entities, citations)
        # 共识信号（功能一）：top-k 命中的高支持度经验点聚合段
        consensus = build_consensus_signal(citations)

        if not citations:
            # 阈值过滤后无相关内容：直接告知，不调 LLM 硬编
            await mark_first_content()
            yield _sse("answer", {"delta": Generator._extractive(query, citations)})
            yield _sse("done", {})
            return

        async with trace_node(rec, "stream-generate", "LLM") as n:
            n.set_extra(deep_thinking=req.deep_thinking,
                        profile_delta=(prof or {}).get("delta", {}) if profile_task is not None else {},
                        query_entities=sorted(query_entities),
                        entity_hint=bool(entity_hint),
                        consensus=bool(consensus),
                        user_context=bool(user_context))
            # 提问者公开背景（贴吧历史发帖画像）并入生成侧画像提示
            gen_profile_prompt = profile_prompt(cur_profile)
            if user_context:
                gen_profile_prompt = (
                    f"{gen_profile_prompt}\n提问者公开背景（来自其历史发帖，供个性化参考，"
                    f"不要复述原文，不要据此编造事实）：{user_context}"
                ).strip()
            async for kind, delta in gen.stream_generate(
                query, citations, deep_thinking=req.deep_thinking,
                memory_messages=build_memory_messages(summary, history),
                intent_prompt=intent.gen_prompt,
                profile_prompt=gen_profile_prompt,
                entity_hint=entity_hint,
                consensus=consensus,
            ):
                if delta:
                    if kind in ("thinking", "answer"):
                        await mark_first_content()
                    yield _sse(kind, {"delta": delta})
        yield _sse("done", {})

    async def traced_stream():
        """包一层 Run 生命周期：正常 SUCCESS、客户端断开 CANCELLED、异常 ERROR。"""
        if rec is None:
            async for chunk in event_stream():
                yield chunk
            return
        try:
            async for chunk in event_stream():
                yield chunk
            await rec.finish("SUCCESS")
        except (asyncio.CancelledError, GeneratorExit):
            await rec.finish("CANCELLED")
            raise
        except Exception as e:  # noqa: BLE001
            await rec.finish("ERROR", f"{type(e).__name__}: {e}")
            raise

    return StreamingResponse(
        traced_stream(), media_type="text/event-stream",
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


def _save_session_db(sid: str, uid: int | None, did: str | None, req: SyncSessionIn) -> int:
    """会话全量覆盖入库（前端是会话内容的权威来源）。返回 updated_at。"""
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
            " ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, updated_at=EXCLUDED.updated_at,"
            " user_id=COALESCE(chat_sessions.user_id, EXCLUDED.user_id),"
            " device_id=COALESCE(chat_sessions.device_id, EXCLUDED.device_id)",
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
    return now_ms


@app.put("/api/sessions/{sid}")
async def save_session(
    sid: str,
    req: SyncSessionIn,
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> dict:
    if req.id != sid:
        raise HTTPException(400, "会话 id 不一致")
    uid, did = _sync_subject(user, device_id)
    now_ms = await run_in_threadpool(_save_session_db, sid, uid, did, req)
    # 会话记忆：保存后异步触发摘要压缩（对齐 ragent 的 append → compressIfNeeded）。
    # 消息此时已确定入库，压缩窗口基于 chat_messages 计算
    gen: Generator = _state["generator"]
    if gen.enabled:
        task = asyncio.create_task(compress_if_needed(sid, gen))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
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


# ---------------- 分享链接（会话快照，公开只读） ----------------

_SHARE_MAX_SESSIONS = 20          # 一次最多分享的会话数
_SHARE_MAX_MESSAGES = 200         # 快照最多消息条数
_SHARE_MAX_MSG_CHARS = 20000      # 单条消息字符上限（超出截断）
_SHARE_MAX_CITATIONS = 20         # 单条回答最多保留的引用数


def _share_citation(c: Any) -> dict:
    """引用瘦身：只留展示与跳转原文所需字段，避免快照过大。"""
    if not isinstance(c, dict):
        return {}
    return {
        "chunk_id": str(c.get("chunk_id") or ""),
        "book_id": c.get("book_id"),
        "book_title": str(c.get("book_title") or ""),
        "chapter_title": str(c.get("chapter_title") or ""),
        "author": c.get("author"),
        "doc_id": c.get("doc_id"),
        "para_ids": [str(p) for p in (c.get("para_ids") or [])][:20],
        "text": str(c.get("text") or "")[:800],
    }


@app.post("/api/shares")
def create_share(req: ShareCreateIn, user: dict | None = Depends(optional_user)) -> dict:
    """生成分享链接：把选中的会话存成快照（不含深度思考），返回可公开访问的 id。"""
    if not req.sessions:
        raise HTTPException(400, "请先选择要分享的对话")
    if len(req.sessions) > _SHARE_MAX_SESSIONS:
        raise HTTPException(400, f"一次最多分享 {_SHARE_MAX_SESSIONS} 组对话")

    sessions: list[dict] = []
    total_msgs = 0
    for s in req.sessions:
        msgs: list[dict] = []
        for m in s.messages:
            if m.role not in ("user", "assistant"):
                continue
            content = (m.content or "").strip()
            if not content:
                continue
            if len(content) > _SHARE_MAX_MSG_CHARS:
                content = content[:_SHARE_MAX_MSG_CHARS] + "\n\n……（内容过长，已截断）"
            msgs.append({
                "role": m.role,
                "content": content,
                "query": (m.query or None) if m.role == "assistant" else None,
                "citations": [_share_citation(c) for c in (m.citations or [])[:_SHARE_MAX_CITATIONS]],
            })
        if not msgs:
            continue
        total_msgs += len(msgs)
        sessions.append({
            "title": (s.title or "对话")[:80],
            "createdAt": s.created_at,
            "messages": msgs,
        })
        if total_msgs >= _SHARE_MAX_MESSAGES:
            break
    if not sessions:
        raise HTTPException(400, "没有可分享的内容")

    share_id = secrets.token_hex(9)          # 18 位十六进制，不可枚举
    uid = user["id"] if user else None
    did = None if user else (req.device_id or None)
    title = (req.title or sessions[0]["title"] or "戒色知识库对话")[:80]
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO share_links (id, title, payload, user_id, device_id)"
            " VALUES (%s, %s, %s::jsonb, %s, %s) RETURNING created_at",
            (share_id, title, json.dumps({"sessions": sessions}, ensure_ascii=False), uid, did),
        )
        created_at = cur.fetchone()["created_at"]
    return {"id": share_id, "title": title, "createdAt": created_at.isoformat()}


@app.get("/api/shares/{share_id}")
def get_share(share_id: str) -> dict:
    """公开读取分享快照（无需登录），顺带累加浏览量。"""
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE share_links SET view_count = view_count + 1 WHERE id=%s"
            " RETURNING title, payload, created_at, view_count",
            (share_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "分享不存在或已被删除")
    return {
        "id": share_id,
        "title": row["title"],
        "createdAt": row["created_at"].isoformat(),
        "viewCount": row["view_count"],
        "sessions": (row["payload"] or {}).get("sessions", []),
    }


@app.delete("/api/shares/{share_id}")
def delete_share(
    share_id: str,
    device_id: str | None = None,
    user: dict | None = Depends(optional_user),
) -> dict:
    """作者本人撤销分享链接（登录按账号、游客按设备标识）。"""
    uid, did = _sync_subject(user, device_id)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, device_id FROM share_links WHERE id=%s", (share_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "分享不存在或已被删除")
        owned = (uid is not None and row["user_id"] == uid) or (
            uid is None and row["user_id"] is None and did is not None
            and row["device_id"] == did
        )
        if not owned:
            raise HTTPException(403, "无权删除该分享")
        cur.execute("DELETE FROM share_links WHERE id=%s", (share_id,))
    return {"ok": True}


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


@app.get("/api/admin/intent-nodes")
def admin_list_nodes(_: dict = Depends(admin_required)) -> list[dict]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code, name, parent_code, kind, action, description, examples,"
            " gen_prompt, enabled, builtin, sort_order FROM intent_nodes ORDER BY sort_order, code"
        )
        return [
            {**dict(r), "action": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}")}
            for r in cur.fetchall()
        ]


@app.post("/api/admin/intent-nodes")
def admin_add_node(req: dict, _: dict = Depends(admin_required)) -> dict:
    code = (req.get("code") or "").strip()
    name = (req.get("name") or "").strip()
    if not code or not name:
        raise HTTPException(400, "code 与 name 必填")
    parent = req.get("parent_code") or None
    action = req.get("action") or {"type": "kb", "book_id": None}
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            if parent:
                cur.execute("SELECT 1 FROM intent_nodes WHERE code=%s", (parent,))
                if not cur.fetchone():
                    raise HTTPException(400, "上级目录不存在")
            cur.execute(
                "INSERT INTO intent_nodes (code, name, parent_code, kind, action, description, examples, gen_prompt, sort_order)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (code, name, parent, req.get("kind", "kb"),
                 json.dumps(action, ensure_ascii=False),
                 req.get("description", ""), req.get("examples", ""),
                 req.get("gen_prompt", ""), req.get("sort_order", 100)),
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "code 已存在或写入失败")
    refresh_intent_tree()
    return {"ok": True}


@app.patch("/api/admin/intent-nodes/{code}")
def admin_update_node(code: str, req: dict, _: dict = Depends(admin_required)) -> dict:
    sets, args = [], []
    for field in ("name", "description", "examples", "kind", "enabled", "gen_prompt", "parent_code"):
        if field in req:
            sets.append(f"{field}=%s")
            args.append(req[field])
    if "action" in req:
        sets.append("action=%s")
        args.append(json.dumps(req["action"], ensure_ascii=False))
    if not sets:
        raise HTTPException(400, "无可更新字段")
    if req.get("parent_code") == code:
        raise HTTPException(400, "不能把自己设为上级目录")
    args.append(code)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE intent_nodes SET {', '.join(sets)} WHERE code=%s", args)
        if cur.rowcount == 0:
            raise HTTPException(404, "节点不存在")
    refresh_intent_tree()
    return {"ok": True}


@app.delete("/api/admin/intent-nodes/{code}")
def admin_delete_node(code: str, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM intent_nodes WHERE parent_code=%s", (code,))
        if cur.fetchone()[0] > 0:
            raise HTTPException(400, "该节点含子节点，请先删除或移动子节点")
        cur.execute("DELETE FROM intent_nodes WHERE code=%s", (code,))
    refresh_intent_tree()
    return {"ok": True}


@app.post("/api/admin/intent-nodes/{code}/move")
def admin_move_node(code: str, req: dict, _: dict = Depends(admin_required)) -> dict:
    """拖拽/移动节点：{parent_code, after_code}。after_code 为 null 时插到目标分组最前。
    重新分配同层 sort_order（10, 20, 30...），带环检测（分组不可移入自己的子树）。"""
    parent = req.get("parent_code") or None
    after = req.get("after_code") or None

    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT parent_code FROM intent_nodes WHERE code=%s", (code,))
        node = cur.fetchone()
        if not node:
            raise HTTPException(404, "节点不存在")

        # 环检测：目标位置不能在自己（或自己子孙）之内
        if parent:
            cur.execute("SELECT code, parent_code FROM intent_nodes")
            all_nodes = {r[0]: r[1] for r in cur.fetchall()}
            if parent == code:
                raise HTTPException(400, "不能移动到自己之下")
            walk = parent
            while walk:
                if walk == code:
                    raise HTTPException(400, "不能移动到自己的子树内")
                walk = all_nodes.get(walk)
            if parent not in all_nodes:
                raise HTTPException(400, "目标分组不存在")

        # 目标层的兄弟序列（不含自己），插入后重排 sort_order
        cur.execute(
            "SELECT code FROM intent_nodes"
            " WHERE COALESCE(parent_code, '') = COALESCE(%s, '') AND code != %s"
            " ORDER BY sort_order, code",
            (parent, code),
        )
        siblings = [r[0] for r in cur.fetchall()]
        if after and after in siblings:
            idx = siblings.index(after) + 1
        else:
            idx = 0
        siblings.insert(idx, code)
        for i, cid in enumerate(siblings):
            cur.execute(
                "UPDATE intent_nodes SET parent_code=%s, sort_order=%s WHERE code=%s",
                (parent, (i + 1) * 10, cid),
            )
    refresh_intent_tree()
    return {"ok": True}


# ---- 意图树总开关 ----

@app.get("/api/admin/intent-tree-switch")
def admin_get_tree_switch(_: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key='intent_tree_enabled'")
        row = cur.fetchone()
    return {"enabled": (row["value"] == "1") if row else True}


@app.put("/api/admin/intent-tree-switch")
def admin_set_tree_switch(req: dict, _: dict = Depends(admin_required)) -> dict:
    enabled = "1" if req.get("enabled") else "0"
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('intent_tree_enabled', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (enabled,),
        )
    refresh_intent_enabled()
    return {"ok": True, "enabled": bool(req.get("enabled"))}


# ---- 查询词映射（参考 ragent QueryTermMappingController：分页 / 增删改 + 试算） ----

_MATCH_TYPES = (1, 2, 3, 4)


@app.get("/api/admin/term-mappings")
def admin_list_mappings(
    page: int = 1, size: int = 20, keyword: str = "",
    _: dict = Depends(admin_required),
) -> dict:
    page, size = max(1, page), min(100, max(1, size))
    where, args = "TRUE", []
    if keyword.strip():
        where = "(source_term ILIKE %s OR target_term ILIKE %s OR remark ILIKE %s)"
        kw = f"%{keyword.strip()}%"
        args = [kw, kw, kw]
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM query_term_mappings WHERE {where}", args)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT id, domain, source_term, target_term, match_type, priority, enabled,"
            f" remark, created_at, updated_at FROM query_term_mappings WHERE {where}"
            f" ORDER BY priority, length(source_term) DESC, id LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"total": total, "mappings": rows}


@app.post("/api/admin/term-mappings")
def admin_add_mapping(req: dict, _: dict = Depends(admin_required)) -> dict:
    source = (req.get("source_term") or "").strip()
    target = (req.get("target_term") or "").strip()
    if not source or not target:
        raise HTTPException(400, "源词与目标词必填")
    match_type = req.get("match_type", 1)
    if match_type not in _MATCH_TYPES:
        raise HTTPException(400, "match_type 只能为 1 精确 / 2 前缀 / 3 正则 / 4 整词")
    if match_type == 3:
        try:
            re.compile(source)
        except re.error as e:
            raise HTTPException(400, f"正则表达式无效: {e}") from None
    try:
        priority = int(req.get("priority") or 100)
    except (TypeError, ValueError):
        raise HTTPException(400, "priority 必须是整数") from None
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO query_term_mappings (domain, source_term, target_term,"
                " match_type, priority, enabled, remark) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ((req.get("domain") or "").strip(), source, target, match_type,
                 priority, bool(req.get("enabled", True)),
                 (req.get("remark") or "").strip()),
            )
    except Exception:
        raise HTTPException(409, "保存失败：同一源词与匹配类型已存在") from None
    refresh_term_mappings()
    return {"ok": True}


@app.put("/api/admin/term-mappings/{mid}")
def admin_update_mapping(mid: int, req: dict, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_term, match_type FROM query_term_mappings WHERE id=%s", (mid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "映射不存在")
        # 计算生效值（未传字段沿用现值），便于统一校验
        source = (req.get("source_term") or "").strip() or row["source_term"]
        match_type = req.get("match_type", row["match_type"])
        if match_type not in _MATCH_TYPES:
            raise HTTPException(400, "match_type 只能为 1 精确 / 2 前缀 / 3 正则 / 4 整词")
        if match_type == 3:
            try:
                re.compile(source)
            except re.error as e:
                raise HTTPException(400, f"正则表达式无效: {e}") from None
        sets, args = [], []
        for f in ("source_term", "target_term", "domain", "remark"):
            if f in req:
                sets.append(f"{f}=%s")
                args.append((req[f] or "").strip())
        if "priority" in req:
            sets.append("priority=%s")
            args.append(int(req["priority"] or 100))
        if "enabled" in req:
            sets.append("enabled=%s")
            args.append(bool(req["enabled"]))
        if "match_type" in req:
            sets.append("match_type=%s")
            args.append(match_type)
        if not sets:
            raise HTTPException(400, "无可更新字段")
        sets.append("updated_at=now()")
        args.append(mid)
        try:
            cur.execute(
                f"UPDATE query_term_mappings SET {', '.join(sets)} WHERE id=%s", args
            )
        except Exception:
            raise HTTPException(409, "保存失败：同一源词与匹配类型已存在") from None
    refresh_term_mappings()
    return {"ok": True}


@app.delete("/api/admin/term-mappings/{mid}")
def admin_delete_mapping(mid: int, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM query_term_mappings WHERE id=%s", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "映射不存在")
    refresh_term_mappings()
    return {"ok": True}


@app.get("/api/admin/term-mappings/preview")
def admin_preview_mapping(text: str, _: dict = Depends(admin_required)) -> dict:
    """试算归一化结果（不落库，供管理后台调规则用）。"""
    result, hits = normalize_with_hits(text)
    return {"result": result, "hits": hits}


# ---- 术语与关系（功能二：实体标注与受限关系；运行时缓存即时刷新） ----

_TERM_TYPES = ("症状", "方法", "因素", "概念")
_EDGE_RELS = ("对治", "导致")


def _terms_page(
    page: int, size: int, keyword: str = "", ttype: str = "", enabled_only: bool = False,
) -> dict:
    """术语分页查询；enabled_only=True 供公开浏览页（只展示启用词条）。"""
    page, size = max(1, page), min(100, max(1, size))
    where, args = ["TRUE"], []
    if keyword.strip():
        where.append("term ILIKE %s")
        args.append(f"%{keyword.strip()}%")
    if ttype in _TERM_TYPES:
        where.append("type = %s")
        args.append(ttype)
    if enabled_only:
        where.append("enabled = TRUE")
    where_sql = " AND ".join(where)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM terms WHERE {where_sql}", args)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT term, type, aliases, enabled, created_at FROM terms WHERE {where_sql}"
            f" ORDER BY type, term LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"total": total, "terms": rows}


@app.get("/api/admin/terms")
def admin_list_terms(
    page: int = 1, size: int = 20, keyword: str = "", type: str = "",
    _: dict = Depends(admin_required),
) -> dict:
    return _terms_page(page, size, keyword, type)


@app.post("/api/admin/terms")
def admin_add_term(req: dict, _: dict = Depends(admin_required)) -> dict:
    term = (req.get("term") or "").strip()
    ttype = req.get("type") or "概念"
    if not term:
        raise HTTPException(400, "term 必填")
    if ttype not in _TERM_TYPES:
        raise HTTPException(400, f"type 只能为 {'/'.join(_TERM_TYPES)}")
    aliases = [a.strip() for a in (req.get("aliases") or []) if isinstance(a, str) and a.strip()]
    try:
        with conn_ctx() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO terms (term, type, aliases, enabled) VALUES (%s,%s,%s,%s)",
                (term, ttype, json.dumps(aliases, ensure_ascii=False), bool(req.get("enabled", True))),
            )
    except Exception:
        raise HTTPException(409, "保存失败：该术语已存在") from None
    refresh_entities()
    return {"ok": True}


@app.put("/api/admin/terms/{term}")
def admin_update_term(term: str, req: dict, _: dict = Depends(admin_required)) -> dict:
    sets, args = [], []
    if "type" in req:
        if req["type"] not in _TERM_TYPES:
            raise HTTPException(400, f"type 只能为 {'/'.join(_TERM_TYPES)}")
        sets.append("type=%s")
        args.append(req["type"])
    if "aliases" in req:
        aliases = [a.strip() for a in (req["aliases"] or []) if isinstance(a, str) and a.strip()]
        sets.append("aliases=%s")
        args.append(json.dumps(aliases, ensure_ascii=False))
    if "enabled" in req:
        sets.append("enabled=%s")
        args.append(bool(req["enabled"]))
    if not sets:
        raise HTTPException(400, "无可更新字段")
    args.append(term)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE terms SET {', '.join(sets)} WHERE term=%s", args)
        if cur.rowcount == 0:
            raise HTTPException(404, "术语不存在")
    refresh_entities()
    return {"ok": True}


@app.delete("/api/admin/terms/{term}")
def admin_delete_term(term: str, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM terms WHERE term=%s", (term,))
        if cur.rowcount == 0:
            raise HTTPException(404, "术语不存在")
    refresh_entities()
    return {"ok": True}


def _edges_page(
    page: int, size: int, rel: str = "", keyword: str = "", min_support: int = 0,
) -> dict:
    """受限关系边分页查询；min_support>0 时只保留证据数达标的边（运行期阈值=2）。"""
    page, size = max(1, page), min(100, max(1, size))
    where, args = ["TRUE"], []
    if rel in _EDGE_RELS:
        where.append("rel = %s")
        args.append(rel)
    if keyword.strip():
        where.append("(src_term ILIKE %s OR dst_term ILIKE %s)")
        kw = f"%{keyword.strip()}%"
        args.extend([kw, kw])
    if min_support > 0:
        where.append("jsonb_array_length(evidence) >= %s")
        args.append(min_support)
    where_sql = " AND ".join(where)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM term_edges WHERE {where_sql}", args)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT id, src_term, rel, dst_term, evidence, created_at FROM term_edges"
            f" WHERE {where_sql} ORDER BY jsonb_array_length(evidence) DESC, id LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size],
        )
        rows = [
            {**dict(r), "support": len(r["evidence"] or [])}
            for r in cur.fetchall()
        ]
    return {"total": total, "edges": rows}


@app.get("/api/admin/term-edges")
def admin_list_edges(
    page: int = 1, size: int = 20, rel: str = "", keyword: str = "",
    _: dict = Depends(admin_required),
) -> dict:
    return _edges_page(page, size, rel, keyword)


def _evidence_rows(rows) -> list[dict]:
    """证据 chunk 行归一化：chunks.para_ids 是 TEXT（JSON 字符串），转成数组供前端跳原文。"""
    out = []
    for r in rows:
        d = dict(r)
        pids = d.get("para_ids")
        if isinstance(pids, str):
            try:
                pids = json.loads(pids)
            except ValueError:
                pids = []
        d["para_ids"] = pids if isinstance(pids, list) else []
        out.append(d)
    return out


def _edge_evidence(eid: int) -> list[dict]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT evidence FROM term_edges WHERE id=%s", (eid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "关系边不存在")
        cur.execute(
            "SELECT id, book_id, doc_id, breadcrumb, chapter_title, para_ids,"
            " left(text, 200) AS text FROM chunks WHERE id = ANY(%s) ORDER BY seq",
            (row["evidence"] or [],),
        )
        return _evidence_rows(cur.fetchall())


@app.get("/api/admin/term-edges/{eid}/evidence")
def admin_edge_evidence(eid: int, _: dict = Depends(admin_required)) -> list[dict]:
    return _edge_evidence(eid)


@app.delete("/api/admin/term-edges/{eid}")
def admin_delete_edge(eid: int, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM term_edges WHERE id=%s", (eid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "关系边不存在")
    refresh_entities()
    return {"ok": True}


# ---- 经验共识（功能一：Claim 共识层） ----

def _claims_page(
    page: int, size: int, keyword: str = "", min_support: int = 0,
    topic: str = "", enabled_only: bool = False,
) -> dict:
    """经验共识分页查询；enabled_only=True 供公开浏览页（只展示管理员保留的簇）。"""
    page, size = max(1, page), min(100, max(1, size))
    where, args = ["TRUE"], []
    if keyword.strip():
        where.append("text ILIKE %s")
        args.append(f"%{keyword.strip()}%")
    if min_support > 0:
        where.append("support_chunks >= %s")
        args.append(min_support)
    if topic.strip():
        where.append("topic = %s")
        args.append(topic.strip())
    if enabled_only:
        where.append("enabled = TRUE")
    where_sql = " AND ".join(where)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM claims WHERE {where_sql}", args)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT id, text, support_authors, support_chunks, authors, books, topic,"
            f" enabled, created_at FROM claims WHERE {where_sql}"
            f" ORDER BY support_chunks DESC, id LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size],
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"total": total, "claims": rows}


def _claim_topics(enabled_only: bool = False) -> list[dict]:
    """主题分布（筛选下拉用）：主题 / 总簇数 / 运行期可用簇数（≥3）。"""
    where_sql = " WHERE enabled = TRUE" if enabled_only else ""
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT topic, COUNT(*), COUNT(*) FILTER (WHERE support_chunks >= 3)"
            f" FROM claims{where_sql} GROUP BY topic ORDER BY COUNT(*) DESC"
        )
        return [{"topic": r[0], "count": r[1], "runtime_count": r[2]} for r in cur.fetchall()]


@app.get("/api/admin/claims")
def admin_list_claims(
    page: int = 1, size: int = 20, keyword: str = "", min_support: int = 0,
    topic: str = "", _: dict = Depends(admin_required),
) -> dict:
    return _claims_page(page, size, keyword, min_support, topic)


@app.get("/api/admin/claims/topics")
def admin_claim_topics(_: dict = Depends(admin_required)) -> list[dict]:
    return _claim_topics()


@app.put("/api/admin/claims/{cid}")
def admin_update_claim(cid: int, req: dict, _: dict = Depends(admin_required)) -> dict:
    if "enabled" not in req:
        raise HTTPException(400, "无可更新字段")
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("UPDATE claims SET enabled=%s WHERE id=%s", (bool(req["enabled"]), cid))
        if cur.rowcount == 0:
            raise HTTPException(404, "共识点不存在")
    return {"ok": True}


def _claim_evidence(cid: int) -> list[dict]:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.book_id, c.doc_id, c.breadcrumb, c.chapter_title, c.author,"
            " c.para_ids, left(c.text, 200) AS text"
            " FROM claim_evidence e JOIN chunks c ON c.id = e.chunk_id"
            " WHERE e.claim_id=%s ORDER BY c.seq",
            (cid,),
        )
        return _evidence_rows(cur.fetchall())


@app.get("/api/claims/{cid}/evidence")
def claim_evidence(cid: int) -> dict:
    """共识点证据（前端答案内「共识」胶囊点击查看）：规范表述 + 支持度 + 证据 chunk。

    证据即书中原文，与阅读页同源公开，无需登录。
    """
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, text, support_authors, support_chunks, authors, books, topic, enabled"
            " FROM claims WHERE id=%s",
            (cid,),
        )
        claim = cur.fetchone()
        if not claim:
            raise HTTPException(404, "共识点不存在")
        cur.execute(
            "SELECT c.id, c.book_id, c.doc_id, c.breadcrumb, c.chapter_title, c.author,"
            " c.para_ids, left(c.text, 240) AS text"
            " FROM claim_evidence e JOIN chunks c ON c.id = e.chunk_id"
            " WHERE e.claim_id=%s ORDER BY c.seq LIMIT 50",
            (cid,),
        )
        evidence = _evidence_rows(cur.fetchall())
    return {"claim": dict(claim), "evidence": evidence}


@app.get("/api/admin/claims/{cid}/evidence")
def admin_claim_evidence(cid: int, _: dict = Depends(admin_required)) -> list[dict]:
    return _claim_evidence(cid)


# ---- 知识探索（公开只读）：普通用户浏览知识图谱与经验共识，无任何写入口 ----

@app.get("/api/explore/overview")
def explore_overview() -> dict:
    """浏览页概览：启用的术语数、运行期可用的关系边/共识簇及其分布。"""
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute("SELECT type, COUNT(*) FROM terms WHERE enabled = TRUE GROUP BY type")
        terms_by_type = [{"type": r[0], "count": r[1]} for r in cur.fetchall()]
        cur.execute(
            "SELECT rel, COUNT(*) FROM term_edges"
            " WHERE jsonb_array_length(evidence) >= 2 GROUP BY rel"
        )
        edges_by_rel = [{"rel": r[0], "count": r[1]} for r in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) FROM claims WHERE enabled = TRUE AND support_chunks >= 3"
        )
        claim_total = cur.fetchone()[0]
    terms_by_type.sort(key=lambda x: -x["count"])
    edges_by_rel.sort(key=lambda x: -x["count"])
    return {
        "terms": {"total": sum(t["count"] for t in terms_by_type), "by_type": terms_by_type},
        "edges": {"total": sum(e["count"] for e in edges_by_rel), "by_rel": edges_by_rel},
        "claims": {"total": claim_total},
    }


@app.get("/api/explore/terms")
def explore_terms(
    page: int = 1, size: int = 60, keyword: str = "", type: str = "",
) -> dict:
    """启用的术语（图谱节点），供浏览页筛选关系边。"""
    return _terms_page(page, size, keyword, type, enabled_only=True)


@app.get("/api/explore/edges")
def explore_edges(
    page: int = 1, size: int = 20, rel: str = "", keyword: str = "", min_support: int = 2,
) -> dict:
    """运行期可用的关系边（默认证据 ≥2，与问答检索的过滤阈值一致）。"""
    return _edges_page(page, size, rel, keyword, max(0, min_support))


@app.get("/api/explore/edges/{eid}/evidence")
def explore_edge_evidence(eid: int) -> list[dict]:
    return _edge_evidence(eid)


@app.get("/api/explore/graph")
def explore_graph(rel: str = "", keyword: str = "", limit: int = 60) -> dict:
    """图谱可视化数据：按证据数取 Top-N 关系边，附带节点类型与度（供 ECharts 力导向图）。"""
    limit = max(8, min(150, limit))
    where, args = ["jsonb_array_length(evidence) >= 2"], []
    if rel in _EDGE_RELS:
        where.append("rel = %s")
        args.append(rel)
    if keyword.strip():
        where.append("(src_term ILIKE %s OR dst_term ILIKE %s)")
        kw = f"%{keyword.strip()}%"
        args.extend([kw, kw])
    where_sql = " AND ".join(where)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, src_term, rel, dst_term, jsonb_array_length(evidence) AS support"
            f" FROM term_edges WHERE {where_sql}"
            " ORDER BY support DESC, id LIMIT %s",
            [*args, limit],
        )
        rows = [dict(r) for r in cur.fetchall()]
        names = {r["src_term"] for r in rows} | {r["dst_term"] for r in rows}
        types: dict[str, str] = {}
        if names:
            cur.execute("SELECT term, type FROM terms WHERE term = ANY(%s)", (list(names),))
            types = {r[0]: r[1] for r in cur.fetchall()}

    degrees: dict[str, int] = {}
    support: dict[str, int] = {}
    for r in rows:
        for name in (r["src_term"], r["dst_term"]):
            degrees[name] = degrees.get(name, 0) + 1
            support[name] = max(support.get(name, 0), r["support"])

    nodes = [
        {
            "name": name,
            "type": types.get(name) or "概念",
            "degree": degrees[name],
            "support": support[name],
        }
        for name in degrees
    ]
    nodes.sort(key=lambda n: (-n["degree"], -n["support"], n["name"]))
    links = [
        {"id": r["id"], "source": r["src_term"], "target": r["dst_term"],
         "rel": r["rel"], "support": r["support"]}
        for r in rows
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/explore/claims")
def explore_claims(
    page: int = 1, size: int = 10, keyword: str = "", min_support: int = 3,
    topic: str = "",
) -> dict:
    """启用的经验共识簇（默认仅 ≥3 处原文支持，与问答共识信号一致）。"""
    return _claims_page(page, size, keyword, max(0, min_support), topic, enabled_only=True)


@app.get("/api/explore/claims/topics")
def explore_claim_topics() -> list[dict]:
    return _claim_topics(enabled_only=True)


@app.get("/api/explore/claims/{cid}/evidence")
def explore_claim_evidence(cid: int) -> list[dict]:
    return _claim_evidence(cid)


# ---- 链路追踪查询（参考 ragent RagTraceController：Run 分页 / 详情含节点） ----

_TRACE_STATUSES = ("RUNNING", "SUCCESS", "ERROR", "CANCELLED")


@app.get("/api/admin/traces/runs")
def admin_trace_runs(
    page: int = 1, size: int = 20, status: str = "", keyword: str = "",
    _: dict = Depends(admin_required),
) -> dict:
    page, size = max(1, page), min(100, max(1, size))
    where, args = ["TRUE"], []
    if status in _TRACE_STATUSES:
        where.append("r.status = %s")
        args.append(status)
    if keyword.strip():
        kw = f"%{keyword.strip()}%"
        where.append("(r.trace_id ILIKE %s OR r.session_id ILIKE %s"
                     " OR r.extra_data->>'question' ILIKE %s)")
        args.extend([kw, kw, kw])
    where_sql = " AND ".join(where)
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM trace_runs r WHERE {where_sql}", args,
        )
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT r.trace_id, r.trace_name, r.entry_method, r.session_id, r.status,"
            f" r.error_message, r.duration_ms, r.start_time, r.end_time,"
            f" r.extra_data->>'question' AS question,"
            f" (SELECT n.duration_ms FROM trace_nodes n"
            f"  WHERE n.trace_id = r.trace_id AND n.node_name = 'user-first-packet'"
            f"  LIMIT 1) AS ttft_ms"
            f" FROM trace_runs r WHERE {where_sql}"
            f" ORDER BY r.start_time DESC LIMIT %s OFFSET %s",
            [*args, size, (page - 1) * size],
        )
        runs = [dict(r) for r in cur.fetchall()]
    return {"total": total, "runs": runs}


@app.get("/api/admin/traces/runs/{trace_id}")
def admin_trace_detail(trace_id: str, _: dict = Depends(admin_required)) -> dict:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.*, r.extra_data->>'question' AS question,"
            " (SELECT n.duration_ms FROM trace_nodes n"
            "  WHERE n.trace_id = r.trace_id AND n.node_name = 'user-first-packet'"
            "  LIMIT 1) AS ttft_ms"
            " FROM trace_runs r WHERE r.trace_id=%s",
            (trace_id,),
        )
        run = cur.fetchone()
        if not run:
            raise HTTPException(404, "trace 不存在")
        cur.execute(
            "SELECT node_id, parent_node_id, depth, node_type, node_name, status,"
            " error_message, duration_ms, start_time, end_time, extra_data"
            " FROM trace_nodes WHERE trace_id=%s ORDER BY start_time, id",
            (trace_id,),
        )
        nodes = [dict(r) for r in cur.fetchall()]
    return {"run": dict(run), "nodes": nodes}


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
