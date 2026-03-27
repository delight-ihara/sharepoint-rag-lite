"""API エンドポイントのユニットテスト（Phase 0.1）"""

from unittest.mock import MagicMock, patch

import pytest


class TestInputValidation:
    """入力バリデーションのテスト"""

    def test_empty_message_rejected(self, client):
        resp = client.post("/chat", json={"message": ""})
        assert resp.status_code == 422

    def test_blank_message_rejected(self, client):
        resp = client.post("/chat", json={"message": "   "})
        assert resp.status_code == 422

    def test_missing_message_rejected(self, client):
        resp = client.post("/chat", json={})
        assert resp.status_code == 422

    def test_too_long_message_rejected(self, client):
        resp = client.post("/chat", json={"message": "あ" * 2001})
        assert resp.status_code == 422

    def test_max_length_message_accepted(self, client):
        """2000文字ちょうどは受け付ける（検索・LLM はモック）"""
        with patch("src.api.hybrid_search", return_value=[]), \
             patch("src.api.generate_answer", return_value={"answer": "test", "citations": []}), \
             patch("src.api._get_conversation_history", return_value=[]), \
             patch("src.api._save_conversation"), \
             patch("src.api._save_query_log"):
            resp = client.post("/chat", json={"message": "あ" * 2000})
            assert resp.status_code == 200

    def test_long_session_id_rejected(self, client):
        resp = client.post("/chat", json={"message": "test", "session_id": "x" * 101})
        assert resp.status_code == 422


class TestChatEndpoint:
    """チャットエンドポイントの正常系・異常系テスト"""

    def test_successful_chat(self, client):
        mock_chunks = [{"chunk_id": "1", "chunk": "test content", "title": "doc.pdf",
                        "source_url": "https://example.com", "category": "01_keiei",
                        "score": 0.9, "reranker_score": 0.9}]
        mock_answer = {"answer": "テスト回答 [1]", "citations": [{"index": 1, "title": "doc.pdf", "source_url": "https://example.com"}]}

        with patch("src.api.hybrid_search", return_value=mock_chunks), \
             patch("src.api.generate_answer", return_value=mock_answer), \
             patch("src.api._get_conversation_history", return_value=[]), \
             patch("src.api._save_conversation"), \
             patch("src.api._save_query_log"):
            resp = client.post("/chat", json={"message": "テスト質問"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["answer"] == "テスト回答 [1]"
            assert len(data["citations"]) == 1
            assert data["session_id"]  # 非空

    def test_search_failure_returns_503(self, client):
        with patch("src.api.hybrid_search", side_effect=Exception("DB connection failed")), \
             patch("src.api._get_conversation_history", return_value=[]):
            resp = client.post("/chat", json={"message": "テスト"})
            assert resp.status_code == 503

    def test_llm_failure_returns_503(self, client):
        with patch("src.api.hybrid_search", return_value=[]), \
             patch("src.api.generate_answer", side_effect=Exception("OpenAI timeout")), \
             patch("src.api._get_conversation_history", return_value=[]):
            resp = client.post("/chat", json={"message": "テスト"})
            assert resp.status_code == 503

    def test_history_failure_non_fatal(self, client):
        """会話履歴取得失敗は非致命的（回答は返る）"""
        with patch("src.api._get_conversation_history", side_effect=Exception("DB error")), \
             patch("src.api.hybrid_search", return_value=[]), \
             patch("src.api.generate_answer", return_value={"answer": "回答", "citations": []}), \
             patch("src.api._save_conversation"), \
             patch("src.api._save_query_log"):
            resp = client.post("/chat", json={"message": "テスト"})
            assert resp.status_code == 200

    def test_save_failure_non_fatal(self, client):
        """会話保存・ログ保存の失敗は非致命的"""
        with patch("src.api.hybrid_search", return_value=[]), \
             patch("src.api.generate_answer", return_value={"answer": "回答", "citations": []}), \
             patch("src.api._get_conversation_history", return_value=[]), \
             patch("src.api._save_conversation", side_effect=Exception("DB write error")), \
             patch("src.api._save_query_log", side_effect=Exception("DB write error")):
            resp = client.post("/chat", json={"message": "テスト"})
            assert resp.status_code == 200


class TestHealthEndpoint:
    """ヘルスチェックのテスト"""

    def test_health_ok(self, client):
        mock_conn = MagicMock()
        with patch("src.api.get_conn", return_value=mock_conn), \
             patch("src.api.put_conn"):
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    def test_health_db_down(self, client):
        with patch("src.api.get_conn", side_effect=Exception("Connection refused")):
            resp = client.get("/health")
            assert resp.status_code == 503
            assert resp.json()["status"] == "unhealthy"


class TestUserEmailExtraction:
    """ユーザーメール取得ロジックのテスト"""

    def test_easyauth_header_takes_priority(self, client):
        with patch("src.api.hybrid_search", return_value=[]) as mock_search, \
             patch("src.api.generate_answer", return_value={"answer": "test", "citations": []}), \
             patch("src.api._get_conversation_history", return_value=[]), \
             patch("src.api._save_conversation"), \
             patch("src.api._save_query_log"):
            resp = client.post(
                "/chat",
                json={"message": "test", "user_email": "body@example.com"},
                headers={"x-ms-client-principal-name": "SSO@Example.COM"},
            )
            assert resp.status_code == 200
            # hybrid_search should have been called with SSO email (lowercased)
            mock_search.assert_called_once()
            call_args = mock_search.call_args
            assert call_args.kwargs.get("user_groups") == ["sso@example.com"] or \
                   call_args[1].get("user_groups") == ["sso@example.com"]

    def test_anonymous_fallback(self, client):
        with patch("src.api.hybrid_search", return_value=[]) as mock_search, \
             patch("src.api.generate_answer", return_value={"answer": "test", "citations": []}), \
             patch("src.api._get_conversation_history", return_value=[]), \
             patch("src.api._save_conversation"), \
             patch("src.api._save_query_log"):
            resp = client.post("/chat", json={"message": "test"})
            assert resp.status_code == 200
            mock_search.assert_called_once()
            call_args = mock_search.call_args
            assert call_args.kwargs.get("user_groups") == ["anonymous@local"] or \
                   call_args[1].get("user_groups") == ["anonymous@local"]
