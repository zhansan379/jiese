-- pgvector 扩展：db 容器首次初始化（空数据卷）时由 postgres 入口脚本自动执行。
-- embedding 列由 backend/scripts/embed_index.py 按需迁移，知识库表由 scripts/build_kb.py 创建。
CREATE EXTENSION IF NOT EXISTS vector;
