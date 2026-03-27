# SharePoint RAG Lite

SharePoint 文書を ACL 付きでベクトル検索する RAG チャットボット。AI Search を使わないスタートアップ規模向け構成。

## 構成

- **ベクトル DB**: PostgreSQL + pgvector
- **API**: Python / FastAPI
- **LLM**: Azure OpenAI (GPT-4o-mini)
- **ホスティング**: Azure Container Apps (Consumption)
- **認証**: Entra ID SSO (EasyAuth)
- **ACL**: SharePoint 権限連動（Graph API）

## 特徴

- SharePoint の ACL をそのまま検索に反映（権限のない文書は検索結果に出ない）
- 月額 ~¥100-600（AI Search 構成比 99% 削減）
- 根拠リンク付き回答 + citation 抑制（ファイル名漏洩防止）
- クエリログ（誰が何を聞いたか）
- テスト 34 ケース（ACL 漏洩ゼロ、精度 100%、P95 5.7 秒）

## ドキュメント

| 文書 | パス |
|------|------|
| 要件定義書 | `docs/01-requirements.md` |
| アーキテクチャ設計書 | `docs/02-architecture.md` |
| セキュリティ設計書 | `docs/03-security.md` |
| リソース設計書 + コスト試算 | `docs/04-resource-design.md` |
| 構築ガイド | `docs/10-build-guide.md` |
| テスト仕様書 | `docs/11-test-spec.md` |

## セットアップ

```bash
pip install -r requirements.txt

# .env.local に環境変数を設定（.env.example を参照）
# インジェスト
python -m src.ingest

# API 起動
uvicorn src.api:app --host 0.0.0.0 --port 8000
```

## テスト

```bash
# 環境変数を設定してからテスト実行
export TEST_BOSS_EMAIL="boss@example.com"
export TEST_MEMBER_EMAIL="member@example.com"
export TEST_SALES_EMAIL="sales@example.com"
python run_tests.py
```
