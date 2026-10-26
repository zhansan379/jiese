# 运行时镜像：后端代码 + 前端构建产物一体托管（CI 在 docker build 之前先执行 npm run build）。
# 知识库数据全部在 PostgreSQL（pgvector），backend/data/ 仅构建期使用，不入镜像。
FROM python:3.11-slim

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/static ./static

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
