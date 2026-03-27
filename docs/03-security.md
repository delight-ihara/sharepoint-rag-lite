# セキュリティ設計書（SharePoint RAG Lite）

## 変更履歴

| 版数 | 日付 | 変更内容 |
|------|------|----------|
| 0.1 | 2026-03-26 | 初版作成。既存 03-security.md をベースに Lite 構成向けに改訂 |

---

## 1. 適用範囲と前提

### 1.1 適用範囲

本書は SharePoint RAG Lite PoC のセキュリティ設計を定義する。

- 対象: PostgreSQL（pgvector）・Azure Container Apps・Azure OpenAI・Entra ID・Key Vault
- PoC 構成（10名）の設計を記述

### 1.2 準拠規格・方針

Azure Security Benchmark を参考に構成。PoC のため外部認証は不要。

### 1.3 既存構成との差分

| 項目       | 既存構成                            | Lite 構成                             | セキュリティ影響            |
| -------- | ------------------------------- | ----------------------------------- | ------------------- |
| ACL フィルタ | AI Search OData フィルタ（MS 保守）     | PostgreSQL WHERE 句（自前保守）            | ACL テスト 18 ケースで回帰検知 |
| デ��タストア  | AI Search + Cosmos DB + Blob    | PostgreSQL 1台                       | 攻撃対象面が縮小（利点）        |
| 中間ストレージ  | Blob Storage（メタデータに ACL）        | なし（DB 直接格納）                         | Blob 経由の漏洩経路が消滅（利点） |
| シークレット数  | 9件（AI Search / Cosmos / Blob 等） | 3件（OpenAI / PostgreSQL / Graph API） | 管理対象が減少（利点）         |

---

## 2. データフロー図と Trust Boundary

```
                    ┌─ TB1: Internet ─────────────────────────┐
                    │                                          │
  [ユーザー] ──TLS──→ [Container Apps (API + UI)]              │
                    │         │                                │
                    └─────────┼────────────────────────────────┘
                              │ TB2: API → Backend Services
                    ┌─────────┼────────────────────────────────┐
                    │         ▼                                │
                    │  [FastAPI (クエリ処理 + ACL フィルタ)]       │
                    │    │         │         │                  │
                    │    │TB3      │TB4      │TB5               │
                    │    ▼         ▼         ▼                  │
                    │ [PostgreSQL] [OpenAI] [Graph API]          │
                    │  (pgvector)            │                  │
                    │                       ▼                  │
                    │                 [SharePoint]              │
                    │                                          │
                    │  [Key Vault] ← API から参照                │
                    └─ TB6: Azure サブスクリプション ────────────────┘
```

### Trust Boundary 定義

| ID | 境界 | 説明 |
|----|------|------|
| TB1 | Internet → Container Apps | ユーザーアクセスの入口。Entra ID SSO が必須 |
| TB2 | API → Backend Services | FastAPI からの内部通信 |
| TB3 | API → PostgreSQL | クエリ・ACL フ���ルタ・会話履歴・ログ |
| TB4 | API → Azure OpenAI | エンベディング生成・回答生成 |
| TB5 | API → Graph API → SharePoint | 文書取得・権限取得 |
| TB6 | Azure サブスクリプション境界 | 外部との全体境界 |

既存構成（TB が 8 箇所）から **6 箇所に削減**。コンポーネント統合により攻撃対象面が縮小。

---

## 3. STRIDE 脅威モデル

標準的な脅威（なりすまし、改ざん、情報漏洩等）は Entra ID SSO、TLS、Key Vault、ACL フィルタで緩和。PoC のため正式なペネトレーションテストは実施しない。

---

## 4. 認証

### 4.1 ユーザー認証

| 項目 | PoC | 100-300名時 |
|------|-----|-------------|
| 方式 | Entra ID SSO（OAuth 2.0） | 同左 + 条件付きアクセス |
| 適用箇所 | Container Apps EasyAuth or MSAL | 同左 |
| MFA | なし | 必須 |

### 4.2 サービス間認証

| 経路 | 方式（PoC） | 100-300名時 |
|------|------------|-------------|
| API → Graph API | クライアントシークレット（Entra ID アプリ登録） | 証明書認証 |
| API → Azure OpenAI | API キー（Key Vault 参照） | マネージド ID |
| API → PostgreSQL（Supabase） | 接続文字列（環境変数） | マネージド ID（Azure PostgreSQL 時） |
| API → Key Vault | マネージド ID（RBAC） | 同左 |

### 4.3 Entra ID アプリ登録

既存構成と同一の設定を流用する。

| 設定 | 値 | 備考 |
|------|-----|------|
| テナント種別 | シングルテナント | 自社のみ |
| API 権限 | Sites.Read.All, Files.Read.All（アプリケーション） | 管理者同意が必要 |
| シークレット有効期限 | 6ヶ月 | PoC 終了後に失効 |
| 100-300名時 | Sites.Selected + 証明書認証 | 対象サイト限定（最小権限） |

---

## 5. 認可（ACL）

### 5.1 認可モデル

| 項目 | 設計 |
|------|------|
| 認可 | SP 権限連動 ACL |
| フィルタ | PostgreSQL WHERE 句（配列演算子） |
| 粒度 | フォルダ単位 |

### 5.2 ACL 実装

**インジェスション時**:
1. `ingest.py` が Graph API でフォルダの権限（permissions エンドポイント）を取得（既存 sp_to_blob.py のロジック流用）
2. 明示的権限がある場合: 閲覧可能ユーザーの UPN（メールアドレス）リストを `allowed_groups TEXT[]` に格納
3. 継承権限（明示的権限なし）の場合: `{"*"}` を格納（全員アクセス可を意味する）

**クエリ時**:
1. Container Apps の Entra ID SSO からユーザーのメールアドレスを取得
2. PostgreSQL クエリで ACL フィルタを適用:

```sql
SELECT chunk_text, source_url, title, category
FROM chunks
WHERE (allowed_groups && ARRAY['user@example.com']   -- ユーザーの権限チェック
    OR '*' = ANY(allowed_groups))                     -- 全員アクセス可
  AND embedding <=> $query_embedding < $threshold     -- ベクトル類似度
ORDER BY embedding <=> $query_embedding
LIMIT 10;
```

**citation 抑制**: 既存構成と同一。LLM が「該当する情報が見つかりませんでした」と回答した場合、ファイル名漏洩防止のため citation を空にして返す（llm.py のロジック流用）。

**権限同期**: `ingest.py --incremental` を cron 実行（1時間ごと）。SP の権限変更を検知し `allowed_groups` を更新。

### 5.3 既存構成との ACL 比較

| 項目 | 既存（AI Search） | Lite（pgvector） |
|------|-------------------|-----------------|
| フィルタ構文 | OData: `allowed_groups/any(g: search.in(g, ...))` | SQL: `allowed_groups && ARRAY[...]` |
| ワイルドカード | `g eq '*'` | `'*' = ANY(allowed_groups)` |
| インデックス | AI Search filterable フィールド | GIN インデックス on TEXT[] |
| フィルタ実行タイミング | 検索エンジン内部（ベクトル検索と同時） | SQL WHERE 句（ベクトル検索と同時） |
| 保守責任 | MS | 自前 |
| 回帰検知 | ACL テスト 18 ケース | 同一テスト流用 |

**セキュリティ上の差は**、フィルタの構文と保守責任のみ。フィルタの実行タイミング（検索前）は同一であり、LLM が権限のないドキュメントを見る経路は存在しない。

### 5.4 データ分類と権限マッピング

| SP フォルダ | 機密レベル | ACL 設定 |
|---|---|---|
| 01_経営 | 機密 | 経営層限定 |
| 02_人事労務 | 社内 | 全社員 |
| 03_営業 | 部署限定 | 営業担当者限定 |

---

## 6. ネットワーク

| 項目 | PoC | 100-300名時 |
|------|-----|------------|
| エンドポイント | パブリック | Private Endpoint |
| VNet | なし | VNet 統合 |
| WAF | なし | Azure Front Door or Application Gateway |
| NSG | なし | Container Apps subnet 制御 |

> PoC ではパブリックエンドポイントを使用。期間限定かつ認証必須のため許容。

---

## 7. データ保護

### 7.1 暗号化

| 項目 | 方式 |
|------|------|
| 保存時暗号化（PostgreSQL） | Supabase: AES-256 SSE / Azure PostgreSQL: SSE 自動有効 |
| 転送時暗号化 | TLS 1.2 以上（全経路） |
| Azure OpenAI | SSE（自動） |

### 7.2 シークレット管理

| # | シークレット | 格納先 | 参照元 |
|---|------------|--------|--------|
| 1 | Azure OpenAI API キー | Key Vault | API |
| 2 | PostgreSQL 接続文字列 | Key Vault or 環境変数 | API |
| 3 | Entra ID クライアントシークレット | Key Vault | API（Graph API 用） |

既存構成（9件）から **3件に削減**。AI Search / Cosmos DB / Blob Storage のシークレットが不要になった。

セキュリティ方針:
- 全シークレットは Key Vault に一元化し、環境変数への直書きを禁止
- Key Vault へのアクセスは RBAC モデル
- アプリケーションには読み取り専用ロール（Secrets User）のみ付与

---

## 8. 監査ログ

| ログ種別 | ソース | 内容 | 保持期間（PoC） |
|---|---|---|---|
| クエリログ | PostgreSQL query_logs テーブル | ユーザー・クエリ内容・タイムスタンプ | DB 存続期間 |
| Azure Activity Log | Azure プラットフォーム | リソース操作（作成・変更・削除） | 90日（既定） |
| Entra ID サインインログ | Entra ID | ユーザー認証イベント | 30日（Free） |
| Key Vault 監査ログ | Key Vault | シークレットアクセス記録 | 90日（既定） |

既存構成では Application Insights で集約していたアプリケーションログを、PostgreSQL の query_logs テーブルで代替（F-08 要件と統合）。

---

## 9. インシデント対応

PoC のため正式なインシデント対応プロセスは策定しない。障害時はリソース再作成で対応する。

---

## 10. 関連文書

| 文書 | 内容 |
|------|------|
| 要件定義書 | セキュリティ要件（NF-S01〜S05） |
| アーキテクチャ設計書 | 認証経路・コンポーネント構��・DB 設計 |
| テスト仕様書 | ACL テスト 18 ケース |
| 既存セキュリティ設計書 | [`03-security.md`](https://github.com/YuhtaIhara/sharepoint-rag-azure/blob/main/docs/03-security.md) |

---

��上
