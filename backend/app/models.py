"""Pydantic 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel


class BookOut(BaseModel):
    id: str
    title: str
    author: str
    doc_count: int
    char_count: int


class TocOut(BaseModel):
    id: str
    doc_id: str
    para_id: str | None
    title: str
    level: int
    seq: int


class ParagraphOut(BaseModel):
    id: str
    seq: int
    kind: str
    text: str


class DocOut(BaseModel):
    id: str
    book_id: str
    title: str
    author: str | None
    prev_id: str | None
    next_id: str | None
    paragraphs: list[ParagraphOut]


class CitationOut(BaseModel):
    chunk_id: str
    book_id: str
    book_title: str
    chapter_title: str
    breadcrumb: str
    author: str | None
    doc_type: str
    doc_id: str
    para_ids: list[str]
    text: str
    score: float


class SearchRequest(BaseModel):
    query: str
    k: int | None = None
    book_id: str | None = None   # jwly / jzl；缺省两本书都搜


class SearchResponse(BaseModel):
    query: str
    expanded: bool = True
    results: list[CitationOut]


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    deep_thinking: bool = False   # 深度思考：更系统的生成/更多摘录
    smart_search: bool = False    # 智能搜索：双倍候选 + 同章多样性约束
    book_id: str | None = None    # 限定检索书籍：jwly / jzl；缺省全部
    session_id: str | None = None # 会话 id：服务端据此加载持久化摘要（会话记忆）


class ChatResponse(BaseModel):
    answer: str
    mode: str            # llm | extractive
    query: str           # 实际用于检索的查询（可能经过改写）
    citations: list[CitationOut]


class HealthOut(BaseModel):
    books: int
    docs: int
    paragraphs: int
    chunks: int
    chunks_indexed: int
    llm: bool
    vector: bool


# ---------------- 账号与会话同步 ----------------

class RegisterIn(BaseModel):
    username: str
    password: str
    email: str | None = None      # 预留字段：暂不做验证/找回，仅存储
    device_id: str | None = None  # 注册时认领该设备的游客会话


class LoginIn(BaseModel):
    username: str
    password: str
    device_id: str | None = None  # 登录时认领该设备的游客会话


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None = None
    role: str = "user"


class TokenOut(BaseModel):
    token: str
    user: UserOut


class SyncMessageIn(BaseModel):
    role: str                     # user | assistant
    content: str = ""
    thinking: str | None = None
    query: str | None = None
    citations: list[dict] = []
    thinking_seconds: int | None = None


class SyncSessionIn(BaseModel):
    id: str                       # 前端生成的会话 id（s_xxx），跨端一致
    title: str = "新对话"
    created_at: int               # 毫秒时间戳
    messages: list[SyncMessageIn] = []


class FeedbackIn(BaseModel):
    session_id: str
    msg_index: int
    rating: int                   # 1 赞 / -1 踩 / 0 取消
    device_id: str | None = None  # 游客设备标识（登录用户忽略）

