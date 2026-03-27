"""LLM 回答生成 + citation 抑制

既存 llm.py のロジックを流用。
"""

import logging
import re

from openai import AzureOpenAI

from .config import AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, CHAT_DEPLOYMENT

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは社内文書検索アシスタントです。
以下の検索結果を元に、ユーザーの質問に日本語で回答してください。

ルール:
- 検索結果に含まれる情報のみを使用すること。推測や外部知識は使わない
- 回答の根拠となる文書を [1], [2] のように番号で参照すること
- 検索結果に該当する情報がない場合は「該当する情報が見つかりませんでした」と回答すること
- 簡潔に回答すること"""

# citation 抑制パターン（既存 llm.py から流用）
_NO_RESULT_PATTERNS = [
    "該当する情報が見つかりませんでした",
    "該当する情報は見つかりませんでした",
    "見つかりませんでした",
]


def generate_answer(
    query: str,
    chunks: list[dict],
    conversation_history: list[dict] | None = None,
) -> dict:
    """検索結果から回答を生成"""
    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version="2024-06-01",
    )

    # 検索結果をコンテキストに整形
    if not chunks:
        return {
            "answer": "該当する情報が見つかりませんでした。",
            "citations": [],
        }

    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        context_parts.append(f"[{i}] {chunk['title']}\n{chunk['chunk']}")
    context = "\n\n".join(context_parts)

    # メッセージ構築
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if conversation_history:
        for msg in conversation_history[-4:]:  # 直近4ターンまで
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": f"検索結果:\n{context}\n\n質問: {query}",
    })

    # LLM 呼び出し
    resp = client.chat.completions.create(
        model=CHAT_DEPLOYMENT,
        messages=messages,
        temperature=0.1,
        max_tokens=1000,
    )

    answer = resp.choices[0].message.content or ""

    # citation 抽出
    citations = []
    ref_numbers = set(map(int, re.findall(r"\[(\d+)\]", answer)))
    for num in sorted(ref_numbers):
        if 1 <= num <= len(chunks):
            chunk = chunks[num - 1]
            citations.append({
                "index": num,
                "title": chunk["title"],
                "source_url": chunk["source_url"],
            })

    # citation 抑制（既存 llm.py のロジック流用）
    # 回答全体が「該当なし」の場合のみ参照元を返さない（ファイル名漏洩防止）
    answer_clean = answer.strip().replace(" ", "").replace("\n", "").replace("。", "")
    if any(p.replace("。", "") in answer_clean for p in _NO_RESULT_PATTERNS) and len(answer_clean) < 80:
        citations = []

    return {
        "answer": answer,
        "citations": citations,
    }
