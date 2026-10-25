# 戒色知识库 · RAG 问答与在线阅读

基于《戒为良药》（飞翔）与《戒者录》（前辈 编）两本书构建的 RAG 知识库问答系统：

- **对话式问答**：左侧会话栏（历史持久化）+ 右侧对话主区，支持「深度思考」「智能搜索」模式
- **在线阅读**：两本书完整预览，目录导航、章节翻页
- **引用跳转**：引用卡片展示所用原文，点击"查看原文"跳转到阅读器对应段落并高亮

技术栈：**FastAPI + React 18 (Vite + Ant Design 5 + Framer Motion) + PostgreSQL (Docker) + jieba**

详细设计见 [docs/实现文档.md](docs/实现文档.md)。

## 页面预览

| 对话问答（RAG 答案 + 引用来源） | 在线阅读（目录导航 + 翻页） |
|:---:|:---:|
| ![对话问答](docs/screenshots/chat.png) | ![在线阅读](docs/screenshots/reader.png) |
| **书架** | **管理后台（意图树路由）** |
| ![书架](docs/screenshots/shelf.png) | ![管理后台](docs/screenshots/admin.png) |

## 快速开始

```bash
# 0. 准备 PostgreSQL（本地 Docker，pgvector 镜像亦可）
docker run -d --name postgres -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
psql -h 127.0.0.1 -U postgres -c "CREATE DATABASE jiese"   # 或 docker exec 进入容器创建

# 1. 构建知识库（解析两本 EPUB → 去重 → 分块 → 分词 → PostgreSQL，约 40 秒）
cd backend
pip install -r requirements.txt
python scripts/build_kb.py

# 2. 构建前端（产物输出到 backend/static/，由 FastAPI 直接托管）
cd ../frontend
npm install
npm run build

# 3. 启动服务（API + 前端一体）
cd ../backend
python -m uvicorn app.main:app --port 8000
# 打开 http://127.0.0.1:8000
```

数据库连接通过 `backend/.env` 覆盖（默认 `postgres/postgres@127.0.0.1:5432/jiese`）。

## 启用大模型生成与向量检索（可选）

不配置任何 Key 时系统开箱即用（BM25 检索 + 原文摘录模式）。复制 `backend/.env.example` 为 `backend/.env` 并填写：

```ini
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 任意 OpenAI 兼容端点
OPENAI_API_KEY=你的Key
LLM_MODEL=glm-4-flash          # 启用答案生成 + 多轮查询改写
EMBEDDING_MODEL=embedding-3    # 启用向量混合检索（需重新运行 build_kb.py）
```

## 知识库规模（当前构建）

| 指标 | 数值 |
|---|---|
| 书籍 | 2 本，325 个阅读页，342 万字 |
| 知识块 | 6591 个（均值 571 字），去重后入索引 6434 |
| 段落 | 25239 个（引用跳转锚点） |
| 目录节点 | 455 个（含《戒者录》文件内锚点小节） |

## 目录结构

```
backend/
  app/            # FastAPI 应用（解析/分块/去重/词表/BM25/检索/生成）
  scripts/build_kb.py   # 构建管线
  data/           # 生成物（embeddings.npy 等，不入库）
  static/         # 前端构建产物（npm run build 生成，不入库）
frontend/         # React 源码（antd + framer-motion）
docs/实现文档.md  # 架构与设计文档
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/books` | 书列表 |
| GET | `/api/books/{id}/toc` | 目录树 |
| GET | `/api/docs/{id}` | 阅读页（含段落锚点 ID） |
| POST | `/api/search` | 纯检索 |
| POST | `/api/chat` | 问答（返回答案 + 引用） |
| GET | `/api/health` | 服务状态 |

交互式文档：http://127.0.0.1:8000/docs
