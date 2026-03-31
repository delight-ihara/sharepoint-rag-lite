"""pgvector ベクトル検索 + ACL フィルタ

既存 search.py の OData フィルタを PostgreSQL WHERE 句に置換。
Phase 0.2: tenacity リトライ追加
"""

import logging

import psycopg2
from openai import APIConnectionError, APIStatusError, AzureOpenAI, RateLimitError
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    ACL_ENABLED,
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_KEY,
    EMBEDDING_DEPLOYMENT,
    MAX_SEARCH_RESULTS,
    SIMILARITY_THRESHOLD,
)
from .db import get_conn, put_conn

log = logging.getLogger(__name__)


def _is_transient_openai_error(exc: BaseException) -> bool:
    """リトライ対象の一時的エラーか判定"""
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (502, 503, 504):
        return True
    return False


@retry(
    retry=retry_if_exception(_is_transient_openai_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _get_query_embedding(query: str) -> list[float]:
    """クエリをエンベディング（リトライ付き）"""
    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version="2024-06-01",
    )
    resp = client.embeddings.create(input=[query], model=EMBEDDING_DEPLOYMENT)
    return resp.data[0].embedding


@retry(
    retry=retry_if_exception_type(psycopg2.OperationalError),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _execute_search(embedding: list[float], user_groups: list[str], top: int) -> list[tuple]:
    """DB ベクトル検索実行（リトライ付き）"""
    conn = get_conn()
    try:
        cur = conn.cursor()

        if ACL_ENABLED and user_groups:
            cur.execute(
                """SELECT chunk_id, chunk_text, title, source_url, category,
                          1 - (embedding <=> %s::vector) AS score
                   FROM chunks
                   WHERE (allowed_groups && %s OR '*' = ANY(allowed_groups))
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s""",
                (embedding, user_groups, embedding, top * 2),
            )
            log.info("ACL filter: user_groups=%s", user_groups)
        else:
            cur.execute(
                """SELECT chunk_id, chunk_text, title, source_url, category,
                          1 - (embedding <=> %s::vector) AS score
                   FROM chunks
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s""",
                (embedding, embedding, top * 2),
            )

        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        put_conn(conn)


def hybrid_search(query: str, user_groups: list[str], top: int | None = None) -> list[dict]:
    """ベクトル検索 + ACL フィルタ

    既存構成の search.py と同等のインターフェース。
    AI Search の OData フィルタを PostgreSQL の配列演算子に置換。
    """
    top = top or MAX_SEARCH_RESULTS
    embedding = _get_query_embedding(query)
    rows = _execute_search(embedding, user_groups, top)

    docs = []
    for row in rows:
        score = float(row[5])
        if score < SIMILARITY_THRESHOLD:
            continue
        docs.append({
            "chunk_id": row[0],
            "chunk": row[1],
            "title": row[2],
            "source_url": row[3],
            "category": row[4],
            "score": score,
            "reranker_score": score,
        })

    docs = docs[:top]
    log.info("Search results: %d件 (query=%s, threshold=%.2f)", len(docs), query, SIMILARITY_THRESHOLD)
    return docs
