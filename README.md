---
type: tool
status: active
tags: [tool, ai, rag, azure, sharepoint]
created: 2026-03-27
---
# SharePoint RAG Lite

SP 文書を ACL 付きでベクトル検索する RAG チャットボット（スタートアップ規模向け）。

- **稼働中**: `https://ca-spraglite-poc-jpe.thankfulgrass-f88968aa.japaneast.azurecontainerapps.io`
- **構成**: pgvector (Supabase) + FastAPI + Azure Container Apps + Azure OpenAI + Entra ID SSO
- **設計書**: `docs/` 配下（要件定義・アーキ・セキュリティ・リソース・構築ガイド・テスト仕様）
- **プロジェクト**: [[rag-infrastructure-project]]
