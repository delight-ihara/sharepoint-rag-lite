"""pgvector ベクトル検索 + ACL フィルタ

既存 search.py の OData フィルタを PostgreSQL WHERE 句に置換。
"""

import logging

from openai import AzureOpenAI

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


def _get_query_embedding(query: str) -> list[float]:
    """クエリをエンベディング"""
    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version="2024-06-01",
    )
    resp = client.embeddings.create(input=[query], model=EMBEDDING_DEPLOYMENT)
    return resp.data[0].embedding


def hybrid_search(query: str, user_groups: list[str], top: int | None = None) -> list[dict]:
    """ベクトル検索 + ACL フィルタ

    既存構成の search.py と同等のインターフェース。
    AI Search の OData フィルタを PostgreSQL の配列演算子に置換。
    """
    top = top or MAX_SEARCH_RESULTS
    embedding = _get_query_embedding(query)

    conn = get_conn()
    try:
        cur = conn.cursor()

        if ACL_ENABLED and user_groups:
            # ACL フィルタ: allowed_groups に user_groups のいずれかが含まれる OR ワイルドカード
            cur.execute(
                """SELECT chunk_id, chunk_text, title, source_url, category,
                          1 - (embedding <=> %s::vector) AS score
                   FROM chunks
                   WHERE (allowed_groups && %s OR '*' = ANY(allowed_groups))
                   ORDER BY embedding <=> %s::vector
                   LIMIT %s""",
                (embedding, user_groups, embedding, top * 2),  # 多めに取って後でフィルタ
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
    finally:
        put_conn(conn)

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
            "reranker_score": score,  # リランカーなしの場合はベクトルスコアで代用
        })

    docs = docs[:top]
    log.info("Search results: %d件 (query=%s, threshold=%.2f)", len(docs), query, SIMILARITY_THRESHOLD)
    return docs
