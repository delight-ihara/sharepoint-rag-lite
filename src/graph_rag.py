"""GraphRAG: エンティティ抽出 + グラフ検索 (F-14)

PostgreSQL の entities / relations テーブルを使用。
Neo4j 等の専用グラフ DB は使わない。
"""

import json
import logging

from openai import AzureOpenAI
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, CHAT_DEPLOYMENT
from .db import get_conn, put_conn

log = logging.getLogger(__name__)

_ENTITY_EXTRACTION_PROMPT = """以下のテキストからエンティティ（人名・組織名・プロジェクト名・文書名）と、
それらの関係（authored, approved, belongs_to, mentions 等）を抽出してください。

以下の JSON 形式で出力してください。説明は不要です。
{
  "entities": [{"name": "...", "type": "person|organization|project|document"}],
  "relations": [{"from": "...", "to": "...", "type": "authored|approved|belongs_to|mentions"}]
}

テキストにエンティティがない場合は空のリストを返してください。"""


def _is_transient(exc):
    from openai import APIConnectionError, APIStatusError, RateLimitError
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (502, 503, 504):
        return True
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _extract_entities_llm(text: str) -> dict:
    """LLM でエンティティ・関係を抽出"""
    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version="2024-06-01",
    )
    resp = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _ENTITY_EXTRACTION_PROMPT},
            {"role": "user", "content": text[:3000]},  # トークン制限
        ],
        temperature=0,
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


def extract_and_store(chunk_id: str, chunk_text: str):
    """チャンクからエンティティを抽出し DB に保存

    LLM 障害時はスキップ（エンティティなしで格納。R-06）
    """
    try:
        result = _extract_entities_llm(chunk_text)
    except Exception:
        log.warning("Entity extraction failed for chunk=%s, skipping", chunk_id)
        return

    entities = result.get("entities", [])
    relations = result.get("relations", [])

    if not entities:
        return

    conn = get_conn()
    try:
        cur = conn.cursor()

        # エンティティを upsert（名前+タイプで重複排除）
        entity_ids = {}
        for ent in entities:
            name = ent.get("name", "").strip()
            etype = ent.get("type", "unknown").strip()
            if not name:
                continue
            cur.execute(
                """INSERT INTO entities (name, type)
                   VALUES (%s, %s)
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                (name, etype),
            )
            row = cur.fetchone()
            if row:
                entity_ids[name] = row[0]
            else:
                # 既存のエンティティの ID を取得
                cur.execute("SELECT id FROM entities WHERE name = %s AND type = %s", (name, etype))
                row = cur.fetchone()
                if row:
                    entity_ids[name] = row[0]

        # 関係を保存
        for rel in relations:
            from_name = rel.get("from", "").strip()
            to_name = rel.get("to", "").strip()
            rel_type = rel.get("type", "mentions").strip()
            from_id = entity_ids.get(from_name)
            to_id = entity_ids.get(to_name)
            if from_id and to_id:
                cur.execute(
                    """INSERT INTO relations (from_entity_id, to_entity_id, relation_type, source_chunk_id)
                       VALUES (%s, %s, %s, %s)""",
                    (from_id, to_id, rel_type, chunk_id),
                )

        conn.commit()
        cur.close()
        log.info("GraphRAG: chunk=%s → %d entities, %d relations", chunk_id, len(entity_ids), len(relations))
    finally:
        put_conn(conn)


def graph_search(query_entities: list[str], user_groups: list[str] | None = None) -> list[dict]:
    """エンティティ名から関連チャンクをグラフ探索（1-2 ホップ）

    ACL フィルタも適用（F-07 との整合性）。
    """
    if not query_entities:
        return []

    conn = get_conn()
    try:
        cur = conn.cursor()

        # エンティティ名であいまい検索
        cur.execute(
            """SELECT id, name, type FROM entities
               WHERE name ILIKE ANY(%s)
               LIMIT 10""",
            ([f"%{e}%" for e in query_entities],),
        )
        matched = cur.fetchall()
        if not matched:
            cur.close()
            return []

        entity_ids = [row[0] for row in matched]

        # 1-2 ホップの関連チャンクを取得
        acl_filter = ""
        params: list = [entity_ids, entity_ids]
        if user_groups:
            acl_filter = "AND (c.allowed_groups && %s OR '*' = ANY(c.allowed_groups))"
            params.append(user_groups)

        cur.execute(
            f"""SELECT DISTINCT c.chunk_id, c.chunk_text, c.title, c.source_url, c.category
                FROM relations r
                JOIN chunks c ON c.chunk_id = r.source_chunk_id
                WHERE (r.from_entity_id = ANY(%s) OR r.to_entity_id = ANY(%s))
                {acl_filter}
                LIMIT 5""",
            params,
        )
        rows = cur.fetchall()
        cur.close()

        return [
            {
                "chunk_id": row[0],
                "chunk": row[1],
                "title": row[2],
                "source_url": row[3],
                "category": row[4],
                "score": 0.8,  # グラフ検索のスコアは固定値
                "reranker_score": 0.8,
            }
            for row in rows
        ]
    finally:
        put_conn(conn)


def extract_query_entities(query: str) -> list[str]:
    """クエリからエンティティ候補を抽出（簡易: 固有名詞っぽい単語を抽出）"""
    # 本格的には LLM で抽出するが、コスト抑制のため簡易実装
    # 「誰が」「何が」系のクエリで人名・組織名を返す
    try:
        result = _extract_entities_llm(query)
        return [e["name"] for e in result.get("entities", []) if e.get("name")]
    except Exception:
        return []
