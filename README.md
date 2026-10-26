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

## 增强层构建（可选：共识层 / 实体关系）

三项增强功能开箱即用为降级模式（无数据不生效），按需构建：

```bash
cd backend
python scripts/seed_terms.py --auto data/terms.csv   # 术语表种子（实体加成 + 关系挖掘的基础）
python scripts/backfill_entities.py                  # 全量 chunk 实体打标（检索加权）
python scripts/mine_edges.py                         # 挖掘受限关系（对治/导致，运行时用证据≥2的边）
python scripts/build_claims.py --concurrency 8       # Claim 共识层（LLM 抽取约 40 分钟，断点续跑）
```

## 启用大模型生成与向量检索（可选）

不配置任何 Key 时系统开箱即用（BM25 检索 + 原文摘录模式）。复制 `backend/.env.example` 为 `backend/.env` 并填写：

```ini
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 任意 OpenAI 兼容端点
OPENAI_API_KEY=你的Key
LLM_MODEL=glm-4-flash          # 启用答案生成 + 多轮查询改写
EMBEDDING_MODEL=embedding-3    # 启用向量混合检索（需重新运行 build_kb.py）
```

## 戒色吧 AI 自动回复机器人（可选）

基于 [aiotieba](https://github.com/lumina37/aiotieba/) 拉取戒色吧新帖 → 调用本机 `/api/chat` 完整问答链路（术语归一化 / 意图识别 / 检索 / LLM 生成）→ 自动回帖。意图为闲聊、检索无命中的帖子自动跳过；LLM 生成失败（摘录回退）的帖子记为 failed 下轮重试。所有处理留痕于 `backend/data/tieba_bot.db`。

```bash
cd backend
pip install -r requirements.txt        # aiotieba 固定 4.7.1（v4.8 起官方移除了回帖接口）
python scripts/tieba_bot.py --scan     # 只看会选哪些帖子（不调问答、不发帖、不写状态）
python scripts/tieba_bot.py --once     # 单轮 dry-run：生成回复仅打印，不实际回帖
python scripts/tieba_bot.py --once --post  # 单轮真实回帖（需 .env 配 TIEBA_BDUSS）
python scripts/tieba_bot.py            # 常驻轮询（默认 dry-run；加 --post 才真实回帖）
python scripts/tieba_bot.py --list     # 查看处理记录    --reset 清空记录
```

`TIEBA_BDUSS` 获取方式：浏览器登录贴吧 → F12 → Application → Cookies → 复制 `BDUSS`，写入 `backend/.env`（未配置时强制 dry-run）。频率与过滤均可通过 `.env` 调整（`TIEBA_*` 前缀，默认：单轮最多 2 帖、间隔 120 秒、每日上限 20 帖、只处理 24h 内新帖、跳过热帖/短帖/广告词命中）。完整参数见 `backend/scripts/tieba_bot.py` 文件头。

> ⚠️ 注意：百度对自动化发帖风控较严（aiotieba 官方正是因此移除了发布接口），账号存在被删帖/封禁的风险。请保持默认低频参数，先用 `--scan` 与 dry-run 观察回复质量，确认无误后再 `--post`。

## 部署（GitHub Actions CI/CD → 阿里云 ECS）

推送 `master` 自动执行：**构建前端 → 构建 Docker 镜像 → 推送阿里云 ACR → SSH 到 ECS 拉取镜像并重启**（ECS 与 ACR 同地域时走 VPC 内网拉取，秒级免流量）。Secrets 未配置时 workflow 自动跳过部署步骤（保持绿色），配置完成后每次 push 即自动上线，也可在 Actions 页手动触发。

### 一次性准备

1. **阿里云 ACR 个人版**（免费）：创建命名空间与镜像仓库（仓库名 `jiese`），并在「访问凭证」页设置固定密码；
2. **部署密钥**（专用，不复用个人密钥）：
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/jiese-deploy -N "" -C "jiese-deploy"
   # 把 ~/.ssh/jiese-deploy.pub 内容追加到服务器 /root/.ssh/authorized_keys，然后验证：
   ssh -i ~/.ssh/jiese-deploy -o BatchMode=yes root@<服务器IP> "echo KEY_LOGIN_OK"
   ```
3. **GitHub Secrets**（仓库 Settings → Secrets and variables → Actions）：

| Secret | 必填 | 说明 |
|---|:---:|---|
| `SSH_HOST` | ✅ | 服务器公网 IP |
| `SSH_PRIVATE_KEY` | ✅ | `jiese-deploy` **私钥整文件内容** |
| `ACR_REGISTRY` | ✅ | 访问域名，如 `crpi-xxx.cn-beijing.personal.cr.aliyuncs.com` |
| `ACR_NAMESPACE` | ✅ | ACR 命名空间 |
| `ACR_DOCKER_USERNAME` / `ACR_DOCKER_PASSWORD` | ✅ | ACR「访问凭证」页的固定密码 |
| `SSH_USER` / `SSH_PORT` | | 默认 `root` / `22` |
| `APP_PORT` | | 对外端口，默认 `8080`（80 常被已有业务占用） |

### 服务器首次初始化（仅一次）

代码更新由 CI 全自动，但知识库语料（EPUB）需手动初始化一次：

```bash
cd /opt/jiese            # CI 部署目录，compose 文件由 workflow 自动同步
mkdir books              # 上传两本 EPUB 到此目录（默认文件名见 backend/.env.example）
cat > .env <<'EOF'       # 可选：配置大模型；不配 = BM25 检索 + 原文摘录模式
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_API_KEY=你的Key
LLM_MODEL=glm-4-flash
EOF
docker compose -f docker-compose.prod.yml up -d db
docker compose -f docker-compose.prod.yml run --rm app python scripts/build_kb.py   # 约 40 秒
```

> 若 db 容器拉取 `pgvector/pgvector:pg16` 失败（Docker Hub 境内访问受限），给 dockerd 配置镜像加速，或将该镜像转存到自己的 ACR 后改 compose 里的 image 地址。
> 增强层（实体/共识）构建方式同本地，需把 `backend/data/terms.csv` 一并上传到 `/opt/jiese`。

完成后随便 push 一次（或 Actions 页 Run workflow）触发部署即可上线。启动时若提示「知识库不可用」，即未完成上面初始化。

### 回滚

历史镜像保留在 ACR 与服务器本地（tag `app-<commit 前 8 位>`）：

```bash
docker pull <ACR_REGISTRY>/<ACR_NAMESPACE>/jiese:app-<sha8>   # 服务器本地已有可跳过
docker tag  <ACR_REGISTRY>/<ACR_NAMESPACE>/jiese:app-<sha8> jiese:latest
cd /opt/jiese && docker compose -f docker-compose.prod.yml up -d app
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
