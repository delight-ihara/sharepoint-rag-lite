"""FastAPI エントリポイント

既存の server.js（認証ヘッダー抽出）+ orchestrator.py（クエリ処理）を統合。
Phase 0.4: ストリーミング応答 (F-10) + API バージョニング (F-28)
Phase 0.6: レート制限 (F-17) + リクエスト ID (F-21)
"""

import json
import logging
import time
import uuid
from pathlib import Path

import psycopg2
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import ALLOWED_ORIGINS, APPLICATIONINSIGHTS_CONNECTION_STRING
from .db import get_conn, put_conn

# ── Application Insights (NF-M01) ──
if APPLICATIONINSIGHTS_CONNECTION_STRING:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=APPLICATIONINSIGHTS_CONNECTION_STRING)
        log_ai = logging.getLogger(__name__)
        log_ai.info("Application Insights enabled")
    except ImportError:
        pass  # azure-monitor-opentelemetry がない環境では無視
from .acl import resolve_user_groups
from .llm import generate_answer, generate_answer_stream, rewrite_query
from .search import hybrid_search

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── レート制限 (F-17) ──
def _rate_limit_key(request: Request) -> str:
    """レート制限のキー: EasyAuth ヘッダー > IP"""
    email = request.headers.get("x-ms-client-principal-name", "")
    return email.lower() if email else get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)

app = FastAPI(title="SharePoint RAG Lite", docs_url="/docs", redoc_url=None)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "リクエスト数が制限を超えました。しばらくしてから再度お試しください。"},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ── API バージョニング (F-28) ──
API_VERSION = "v1"
v1 = APIRouter(prefix="/v1", tags=["v1"])

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
    message_id: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


# ── グローバル例外ハンドラ ──

@app.exception_handler(psycopg2.OperationalError)
async def db_operational_handler(request: Request, exc: psycopg2.OperationalError):
    """DB 接続不能（プール生成含む）を 503 で返し、監視・UI の切り分けをしやすくする"""
    log.exception("Database unavailable (OperationalError)")
    msg = str(exc).lower()
    detail = "しばらくしてから再度お試しください。"
    if "tenant" in msg and "not found" in msg:
        detail = (
            "PostgreSQL（Supabase）のテナントが見つかりません。"
            "プロジェクトが削除・一時停止していないか、DATABASE_URL のホストとユーザー（postgres.<project_ref>）をダッシュボードの値と照合してください。"
        )
    return JSONResponse(
        status_code=503,
        content={"error": "データベースに接続できません。", "detail": detail},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": "内部エラーが発生しました。しばらくしてから再度お試しください。"},
    )


# ── リクエスト ID + API-Version ヘッダー (F-21 / F-28) ──

@app.middleware("http")
async def add_request_headers(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["API-Version"] = API_VERSION
    return response


# ── ヘルパー関数 ──

def _get_user_email(request: Request, body: ChatRequest) -> str:
    """Entra ID SSO ヘッダーまたはリクエストボディからユーザーメールを取得"""
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


def _save_conversation(session_id: str, user_email: str, role: str, content: str, citations=None) -> str:
    """会話履歴を保存し、生成された ID を返す"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO conversations (session_id, user_email, role, content, citations)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (session_id, user_email, role, content, json.dumps(citations) if citations else None),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return str(row[0]) if row else ""
    finally:
        put_conn(conn)


def _save_query_log(user_email: str, query: str, chunks_used: int, response_time_ms: int, tokens_used: int = 0):
    """クエリログを保存（F-08 + F-30 メータリング）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO query_logs (user_email, query, chunks_used, tokens_used, response_time_ms)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_email, query, chunks_used, tokens_used, response_time_ms),
        )
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


def _process_chat_pre(request: Request, body: ChatRequest) -> tuple[str, str, list[dict], str, list[dict]]:
    """チャット前処理: 認証 → 履歴 → リライト → 検索（/chat と /chat/stream で共通）"""
    start = time.time()
    user_email = _get_user_email(request, body)
    session_id = body.session_id or str(uuid.uuid4())

    log.info("Chat: user=%s, query=%s", user_email, body.message[:50])

    try:
        history = _get_conversation_history(session_id)
    except Exception:
        log.exception("Failed to load conversation history for session=%s", session_id)
        history = []

    search_query = rewrite_query(body.message, history)

    try:
        try:
            user_groups = resolve_user_groups(user_email)
        except Exception:
            log.warning("グループ解決失敗 (%s) — メールのみで ACL チェック", user_email)
            user_groups = [user_email]
        chunks = hybrid_search(query=search_query, user_groups=user_groups)
    except Exception:
        log.exception("Search failed for query=%s", body.message[:50])
        raise HTTPException(
            status_code=503,
            detail="検索サービスに接続できません。しばらくしてから再度お試しください。",
        )

    return user_email, session_id, history, body.message, chunks


def _process_chat_post(
    session_id: str, user_email: str, query: str, answer: str, chunks: list[dict], start: float, citations: list[dict] | None = None
) -> str:
    """チャット後処理: 会話保存 → ログ保存。assistant メッセージの ID を返す"""
    message_id = ""
    try:
        _save_conversation(session_id, user_email, "user", query)
        message_id = _save_conversation(session_id, user_email, "assistant", answer, citations)
    except Exception:
        log.exception("Failed to save conversation for session=%s", session_id)

    try:
        elapsed_ms = int((time.time() - start) * 1000)
        _save_query_log(user_email, query, len(chunks), elapsed_ms)
    except Exception:
        log.exception("Failed to save query log")

    return message_id


# ── v1 エンドポイント ──

@v1.post("/chat", response_model=ChatResponse, responses={503: {"model": ErrorResponse}})
@limiter.limit("10/minute")
async def chat_v1(request: Request, body: ChatRequest):
    """チャットエンドポイント（JSON レスポンス）"""
    start = time.time()
    user_email, session_id, history, query, chunks = _process_chat_pre(request, body)

    try:
        result = generate_answer(query=query, chunks=chunks, conversation_history=history)
    except Exception:
        log.exception("LLM generation failed for query=%s", query[:50])
        raise HTTPException(
            status_code=503,
            detail="回答生成サービスに接続できません。しばらくしてから再度お試しください。",
        )

    message_id = _process_chat_post(session_id, user_email, query, result["answer"], chunks, start, result["citations"])

    return ChatResponse(
        answer=result["answer"],
        citations=result["citations"],
        session_id=session_id,
        message_id=message_id,
    )


@v1.post("/chat/stream")
@limiter.limit("10/minute")
async def chat_stream_v1(request: Request, body: ChatRequest):
    """ストリーミングチャットエンドポイント（SSE）(F-10)"""
    start = time.time()
    user_email, session_id, history, query, chunks = _process_chat_pre(request, body)

    async def event_stream():
        full_answer = ""
        try:
            for chunk_text in generate_answer_stream(
                query=query, chunks=chunks, conversation_history=history
            ):
                full_answer += chunk_text
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk_text}, ensure_ascii=False)}\n\n"

            # ストリーム完了後に citation を抽出
            from .llm import _extract_citations
            citations = _extract_citations(full_answer, chunks)

            # 後処理
            message_id = _process_chat_post(session_id, user_email, query, full_answer, chunks, start, citations)

            yield f"data: {json.dumps({'type': 'done', 'citations': citations, 'session_id': session_id, 'message_id': message_id}, ensure_ascii=False)}\n\n"

        except Exception as e:
            log.exception("Stream generation failed: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'message': '回答生成中にエラーが発生しました。'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── フィードバック (F-19) ──

class FeedbackRequest(BaseModel):
    session_id: str = Field(..., max_length=100)
    message_id: str = Field(..., max_length=100)
    rating: int = Field(...)  # -1 (thumbs down) or 1 (thumbs up)
    comment: str = Field(default="", max_length=1000)

    @field_validator("rating")
    @classmethod
    def rating_must_be_valid(cls, v: int) -> int:
        if v not in (-1, 1):
            raise ValueError("rating は -1 または 1 のみ")
        return v


@v1.post("/feedback")
async def feedback_v1(request: Request, body: FeedbackRequest):
    """回答フィードバック送信"""
    user_email = request.headers.get("x-ms-client-principal-name", "anonymous@local").lower()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO feedback (session_id, message_id, user_email, rating, comment)
               VALUES (%s, %s, %s, %s, %s)""",
            (body.session_id, body.message_id, user_email, body.rating, body.comment or None),
        )
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)
    return {"status": "ok"}


# ── インデックス状態 API ──

@v1.get("/index/status")
async def index_status_v1():
    """インデックスの状態を返す（ファイル数・チャンク数・最終更新日時）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*), count(DISTINCT file_id), max(updated_at) FROM chunks")
        row = cur.fetchone()
        cur.close()
        return {
            "total_chunks": row[0],
            "total_files": row[1],
            "last_updated": row[2].isoformat() if row[2] else None,
        }
    except Exception:
        return {"total_chunks": 0, "total_files": 0, "last_updated": None}
    finally:
        put_conn(conn)


# ── 管理ダッシュボード (F-27) ──

@v1.get("/admin/stats")
async def admin_stats_v1(request: Request):
    """管理者向け統計 API"""
    # TODO: 管理者ロール判定（Phase 1 で Entra ID ロール確認に変更）
    conn = get_conn()
    try:
        cur = conn.cursor()

        # 利用統計
        cur.execute("""
            SELECT count(*), count(DISTINCT user_email),
                   coalesce(avg(response_time_ms), 0), coalesce(sum(tokens_used), 0)
            FROM query_logs
            WHERE created_at > now() - interval '30 days'
        """)
        stats = cur.fetchone()

        # フィードバック集計
        cur.execute("""
            SELECT
                coalesce(sum(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) AS thumbs_up,
                coalesce(sum(CASE WHEN rating = -1 THEN 1 ELSE 0 END), 0) AS thumbs_down
            FROM feedback
            WHERE created_at > now() - interval '30 days'
        """)
        fb = cur.fetchone()

        # インデックス状態
        cur.execute("SELECT count(*), count(DISTINCT file_id), max(updated_at) FROM chunks")
        idx = cur.fetchone()

        cur.close()

        return {
            "period": "last_30_days",
            "queries": {"total": stats[0], "unique_users": stats[1], "avg_response_ms": round(float(stats[2])), "total_tokens": stats[3]},
            "feedback": {"thumbs_up": fb[0], "thumbs_down": fb[1]},
            "index": {"total_chunks": idx[0], "total_files": idx[1], "last_updated": idx[2].isoformat() if idx[2] else None},
        }
    except Exception:
        log.exception("Admin stats query failed")
        return JSONResponse(status_code=500, content={"error": "統計の取得に失敗しました"})
    finally:
        put_conn(conn)


# ── 会話履歴 API ──

@v1.get("/conversations")
async def conversations_list_v1(request: Request):
    """ユーザーの会話セッション一覧を返す（サイドバー用）"""
    user_email = request.headers.get("x-ms-client-principal-name", "anonymous@local").lower()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT session_id,
                      min(content) FILTER (WHERE role = 'user') AS first_message,
                      min(created_at) AS created_at,
                      count(*) AS message_count
               FROM conversations
               WHERE user_email = %s
               GROUP BY session_id
               ORDER BY min(created_at) DESC
               LIMIT 50""",
            (user_email,),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "session_id": r[0],
                "title": (r[1] or "")[:30],
                "created_at": r[2].isoformat() if r[2] else None,
                "message_count": r[3],
            }
            for r in rows
        ]
    finally:
        put_conn(conn)


@v1.get("/conversations/{session_id}/messages")
async def conversation_messages_v1(request: Request, session_id: str):
    """特定セッションのメッセージ履歴を返す"""
    user_email = request.headers.get("x-ms-client-principal-name", "anonymous@local").lower()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, role, content, citations, created_at
               FROM conversations
               WHERE session_id = %s AND user_email = %s
               ORDER BY created_at
               LIMIT 100""",
            (session_id, user_email),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id": str(r[0]),
                "role": r[1],
                "content": r[2],
                "citations": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
    finally:
        put_conn(conn)


# ── 会話削除 API ──

@v1.delete("/conversations/{session_id}")
async def conversation_delete_v1(request: Request, session_id: str):
    """会話セッションを削除する"""
    user_email = request.headers.get("x-ms-client-principal-name", "anonymous@local").lower()
    conn = get_conn()
    try:
        cur = conn.cursor()
        # フィードバックも削除
        cur.execute(
            "DELETE FROM feedback WHERE session_id = %s AND user_email = %s",
            (session_id, user_email),
        )
        cur.execute(
            "DELETE FROM conversations WHERE session_id = %s AND user_email = %s",
            (session_id, user_email),
        )
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        if deleted == 0:
            raise HTTPException(status_code=404, detail="会話が見つかりません")
        return {"status": "ok", "deleted_messages": deleted}
    finally:
        put_conn(conn)


# ── v1 ルーターを登録 ──
app.include_router(v1)


# ── レガシー互換: /chat → /v1/chat（移行期間用） ──

@app.post("/chat", response_model=ChatResponse, responses={503: {"model": ErrorResponse}}, include_in_schema=False)
async def chat_legacy(request: Request, body: ChatRequest):
    """レガシー互換エンドポイント（/v1/chat と同一動作）"""
    return await chat_v1(request, body)


# ── バージョン非依存エンドポイント ──

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
