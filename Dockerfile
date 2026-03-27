FROM python:3.12-slim

WORKDIR /app

# 依存関係のインストール（キャッシュ活用のため先にコピー）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコード
COPY src/ src/

# non-root ユーザーで実行
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
