"""FastAPI エントリポイント

既存の server.js（認証ヘッダー抽出）+ orchestrator.py（クエリ処理）を統合。
"""

import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .config import ALLOWED_ORIGINS
from .db import get_conn, put_conn
from .llm import generate_answer
from .search import hybrid_search

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="SharePoint RAG Lite")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── 最大入力長 ──
MAX_MESSAGE_LENGTH = 2000


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)
    session_id: str = Field(default="", max_length=100)
    user_email: str = Field(default="", max_length=254)

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("メッセージが空です")
        return v.strip()


class ChatResponse(BaseModel):
    answer: str
    citations: list[dict]
    session_id: str


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


# ── グローバル例外ハンドラ ──

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "内部エラーが発生しました。しばらくしてから再度お試しください。"},
    )


def _get_user_email(request: Request, body: ChatRequest) -> str:
    """Entra ID SSO ヘッダーまたはリクエストボディからユーザーメールを取得

    Azure Container Apps EasyAuth は x-ms-client-principal-name ヘッダーを注入する。
    ローカル開発時はリクエストボディの user_email を使用。
    """
    header_email = request.headers.get("x-ms-client-principal-name", "")
    if header_email:
        return header_email.lower()
    if body.user_email:
        return body.user_email.lower()
    return "anonymous@local"


def _get_conversation_history(session_id: str) -> list[dict]:
    """セッションの会話履歴を取得"""
    if not session_id:
        return []

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT role, content FROM conversations
               WHERE session_id = %s
               ORDER BY created_at
               LIMIT 20""",
            (session_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [{"role": r[0], "content": r[1]} for r in rows]
    finally:
        put_conn(conn)


def _save_conversation(session_id: str, user_email: str, role: str, content: str, citations=None):
    """会話履歴を保存"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO conversations (session_id, user_email, role, content, citations)
               VALUES (%s, %s, %s, %s, %s)""",
            (session_id, user_email, role, content, None),
        )
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


def _save_query_log(user_email: str, query: str, chunks_used: int, response_time_ms: int):
    """クエリログを保存（F-08）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO query_logs (user_email, query, chunks_used, response_time_ms)
               VALUES (%s, %s, %s, %s)""",
            (user_email, query, chunks_used, response_time_ms),
        )
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


@app.post("/chat", response_model=ChatResponse, responses={500: {"model": ErrorResponse}})
async def chat(request: Request, body: ChatRequest):
    """チャットエンドポイント"""
    start = time.time()

    user_email = _get_user_email(request, body)
    session_id = body.session_id or str(uuid.uuid4())

    log.info("Chat: user=%s, query=%s", user_email, body.message[:50])

    try:
        # 1. 会話履歴取得
        history = _get_conversation_history(session_id)
    except Exception:
        log.exception("Failed to load conversation history for session=%s", session_id)
        history = []  # 履歴取得失敗は非致命的、空で続行

    try:
        # 2. ベクトル検索 + ACL フィルタ
        chunks = hybrid_search(
            query=body.message,
            user_groups=[user_email],
        )
    except Exception:
        log.exception("Search failed for query=%s", body.message[:50])
        raise HTTPException(
            status_code=503,
            detail="検索サービスに接続できません。しばらくしてから再度お試しください。",
        )

    try:
        # 3. 回答生成
        result = generate_answer(
            query=body.message,
            chunks=chunks,
            conversation_history=history,
        )
    except Exception:
        log.exception("LLM generation failed for query=%s", body.message[:50])
        raise HTTPException(
            status_code=503,
            detail="回答生成サービスに接続できません。しばらくしてから再度お試しください。",
        )

    # 4. 会話履歴保存（失敗しても回答は返す）
    try:
        _save_conversation(session_id, user_email, "user", body.message)
        _save_conversation(session_id, user_email, "assistant", result["answer"])
    except Exception:
        log.exception("Failed to save conversation for session=%s", session_id)

    # 5. クエリログ保存（失敗しても回答は返す）
    try:
        elapsed_ms = int((time.time() - start) * 1000)
        _save_query_log(user_email, body.message, len(chunks), elapsed_ms)
    except Exception:
        log.exception("Failed to save query log")

    return ChatResponse(
        answer=result["answer"],
        citations=result["citations"],
        session_id=session_id,
    )


@app.get("/health")
async def health():
    """ヘルスチェック（DB 接続確認付き）"""
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        finally:
            put_conn(conn)
        return {"status": "ok"}
    except Exception:
        log.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "unhealthy"})


# Static files (chat UI)
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    @app.get("/")
    async def root():
        return FileResponse(_static_dir / "index.html")
