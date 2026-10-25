"""环境配置：LLM / Embedding / EPUB 路径。全部可通过 backend/.env 覆盖。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI 兼容端点（任意厂商：OpenAI / 智谱 / DeepSeek 等）
    openai_base_url: str = ""
    openai_api_key: str = ""
    llm_model: str = ""

    # 重排序（可选；SiliconFlow /v1/rerank，与 RRF 排名加权融合）
    rerank_model: str = ""
    rerank_weight: float = 0.5
    rerank_threshold: float = 0.1   # rerank 绝对相关性下限：低于此值的候选直接丢弃（0=关闭）

    # 向量检索（可选；不配置则 BM25-only）
    embedding_model: str = ""
    embedding_base_url: str = ""   # 缺省复用 openai_base_url
    embedding_api_key: str = ""    # 缺省复用 openai_api_key

    # PostgreSQL（本地 Docker）
    admin_password: str = "admin12345"   # 内置管理员初始密码

    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_db: str = "jiese"
    pg_user: str = "postgres"
    pg_password: str = "postgres"

    # 语料路径
    epub_jwly: str = r"D:\feng1\Documents\戒为良药 - 飞翔.epub"
    epub_jzl: str = r"D:\feng1\Documents\戒者录 - 前辈.epub"

    # 检索参数
    top_k: int = 6
    candidate_k: int = 24

    # 会话记忆（参考 ragent：最近 N 轮原文 + 持久化摘要，控制长对话的上下文成本）
    memory_history_turns: int = 8        # 保留原文的最近轮数（一轮 = 一条 user + 其 assistant 回复）
    memory_summary_enabled: bool = True  # 摘要压缩开关（LLM 未配置时自动跳过）
    memory_summary_start_turns: int = 9  # user 消息数达到该值后才开始生成摘要
    memory_summary_max_chars: int = 200  # 摘要最大字符数
    memory_context_turns: int = 2        # 生成回答时注入的最近轮数

    # 链路追踪（参考 ragent RagTraceProperties：采集开关 + 错误截断长度）
    trace_enabled: bool = True
    trace_max_error_length: int = 1000

    @property
    def dsn(self) -> dict:
        return {
            "host": self.pg_host, "port": self.pg_port,
            "dbname": self.pg_db, "user": self.pg_user,
            "password": self.pg_password,
        }

    @property
    def data_dir(self) -> Path:
        return BACKEND_DIR / "data"

    @property
    def emb_matrix_path(self) -> Path:
        return self.data_dir / "embeddings.npy"

    @property
    def emb_ids_path(self) -> Path:
        return self.data_dir / "embedding_chunk_ids.json"

    @property
    def rerank_enabled(self) -> bool:
        return bool(self.rerank_model and self.openai_api_key and self.openai_base_url)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key and self.llm_model and self.openai_base_url)

    @property
    def emb_base(self) -> str:
        return self.embedding_base_url or self.openai_base_url

    @property
    def emb_key(self) -> str:
        return self.embedding_api_key or self.openai_api_key


settings = Settings()
