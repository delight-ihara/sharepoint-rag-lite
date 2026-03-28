# リソース設計書 + コスト試算（SharePoint RAG Lite）

## 変更履歴

| 版数 | 日付 | 変更内容 |
|------|------|----------|
| 0.1 | 2026-03-26 | 初版作成 |
| 0.2 | 2026-03-27 | Phase 0: Application Insights・GraphRAG テーブル追加、コスト更新 |

---

## シート1: 命名規則

### 命名体系

[Azure CAF 命名規則](https://learn.microsoft.com/ja-jp/azure/cloud-adoption-framework/ready/azure-best-practices/resource-naming) に準拠。

```
{CAF略称}-{ワークロード}-{環境}-{リージョン}
```

| 要素 | 値 | 説明 |
|------|-----|------|
| ワークロード | `spraglite` | SharePoint RAG Lite |
| 環境 | `poc` | PoC 環境（本番時は `prd`） |
| リージョン | `jpe` | Japan East |

### リソース名一覧

| # | サービス | CAF 略称 | リソース名 | 備考 |
|---|---------|---------|-----------|------|
| 0 | Resource Group | `rg` | `<YOUR_RESOURCE_GROUP>` | — |
| 1 | Entra ID アプリ | — | 既存 `<YOUR_ENTRA_APP>` を流用 | 新規作成不要 |
| 2 | Azure OpenAI | `oai` | 既存 `<YOUR_OPENAI_RESOURCE>` を流用 | GPT-4o-mini + embedding-small を追加デプロイ |
| 3 | Key Vault | `kv` | `<YOUR_KEY_VAULT>` | シークレット 3 件 |
| 4 | Container Apps Environment | `cae` | `<YOUR_CONTAINER_ENV>` | Consumption プラン |
| 5 | Container App | `ca` | `<YOUR_CONTAINER_APP>` | FastAPI アプリ |
| 7 | Application Insights | `appi` | `<YOUR_APP_INSIGHTS>` | 監視・テレメトリ |

**Azure 外リソース（PoC）**:

| # | サービス | 名称 | 備考 |
|---|---------|------|------|
| 6 | `<YOUR_SUPABASE_PROJECT>`（Supabase） | PostgreSQL + pgvector | Free | 500MB / ap-northeast-1 | ベクトル DB + 会話履歴 + クエリログ + GraphRAG | — |
| 7 | `<YOUR_APP_INSIGHTS>` | Application Insights | — | Japan East / OpenTelemetry SDK | 監視・アラート | #4 |

### タギング戦略

全 Azure リソースに以下の必須タグを付与���

| タグ名 | 値 | 用途 |
|--------|-----|------|
| `Environment` | `poc` | 環境識別 |
| `Project` | `sp-rag-lite` | コスト配賦 |
| `CreatedDate` | 作成日 | PoC 終了判断用 |

---

## シート2: リソース一覧

基本リージョン: Japan East。例外: Azure OpenAI → East US 2（JE 非対応）

| # | リソース名 | サービス | SKU | 設定値 | 用途 | 依存先 |
|---|-----------|---------|-----|--------|------|--------|
| 0 | `<YOUR_RESOURCE_GROUP>` | Resource Group | — | Japan East | 全���ソースの入れ物 | — |
| 1 | `<YOUR_ENTRA_APP>`（流用） | Entra ID アプリ登録 | — | Sites.Read.All, Files.Read.All | Graph API 認証 | — |
| 2 | `<YOUR_OPENAI_RESOURCE>`（流用） | Azure OpenAI | S0 | GPT-4o-mini: 30K TPM / text-embedding-3-small: 120K TPM / East US 2 | 回答生成 + 埋め込み | — |
| 3 | `<YOUR_KEY_VAULT>` | Key Vault | Standard | RBAC / シークレット 3 件 | シークレット管理 | — |
| 4 | `<YOUR_CONTAINER_ENV>` | Container Apps Env | Consumption | Japan East | Container Apps 実行環境 | — |
| 5 | `<YOUR_CONTAINER_APP>` | Container App | Consumption | Python 3.12 / min=0 max=1 / Entra ID EasyAuth | API + チャット UI | #3, #4 |
| 6 | `<YOUR_SUPABASE_PROJECT>`（Supabase） | PostgreSQL + pgvector | Free | 500MB / ap-northeast-1 | ベク���ル DB + 会話履歴 + クエリログ | — |

### 既存構成との比較

| 項目 | 既存構成（12リソース） | Lite 構成（7リソース） |
|------|---------------------|---------------------|
| AI Search S1 | ○ | **削除** |
| Cosmos DB | ○ | **削除**（PostgreSQL に統合） |
| Blob Storage × 2 | ○ | **削除** |
| Document Intelligence | ○ | **削除** |
| Cognitive Services | ○ | **削除** |
| App Service B1 | ○ | **削除**（Container Apps に統合） |
| Azure Functions | ○ | **削除**（Container Apps に統合） |
| Application Insights | ○ | **新規**（OpenTelemetry SDK で統合） |
| Azure OpenAI | ○ | **流用** |
| Entra ID アプリ | ○ | **流用** |
| Key Vault | ○ | **新規**（既存と別名） |
| Container Apps | — | **新規** |
| Supabase | — | **新規**（Azure 外） |

### RBAC 設定

| 付与先 | 対象リソース | ロール |
|--------|------------|--------|
| Container App MI | Key Vault (#3) | Key Vault Secrets User |
| Container App MI | Azure OpenAI (#2) | Cognitive Services OpenAI User |
| 自分自身 | Key Vault (#3) | Key Vault Secrets Officer |

既存構成（10件）から **3件に削減**。

### Key Vault シークレット

| シークレット名 | ソース |
|---|---|
| `AZURE-OPENAI-KEY` | #2 API キー |
| `GRAPH-CLIENT-SECRET` | #1 シークレット値 |
| `DATABASE-URL` | #6 Supabase 接続文字列 |

既存構成（9件）から **3件に削減**。OpenAI エンドポイント・Graph テナント ID 等は環境変数で十分（秘匿不要）。

---

## シート3: コスト試算

### 前提条件

| 項目 | 値 |
|------|-----|
| ユーザー数 | 10名 |
| SP 文書数 | 100件以上 |
| クエリ数 | ~50/日（10名 × 5回） |
| リージョン | Japan East |
| 通貨 | USD（参考: 1 USD ≒ 150 JPY） |

### PoC 構成 月額コスト内訳

| # | リソース | SKU | 課金種別 | 月額（USD） | 月額（JPY） | 備考 |
|---|---------|-----|---------|------------|-----------|------|
| 1 | **Azure OpenAI (GPT-4o-mini)** | Standard | 従量 | ~$0.40 | ~¥60 | 1,500クエリ/月 × 800トークン |
| 2 | **Azure OpenAI (embedding-small)** | Standard | 従量 | ~$0.01 | ~¥2 | 初回+差分。100文書 = ~200Kトークン |
| 3 | **Container Apps** | Consumption | 従量 | ~$0-3 | ~¥0-450 | スケールゼロ。月180K vCPU-s 無料枠内 |
| 4 | **Key Vault** | Standard | 従量 | ~$0.01 | ~¥2 | $0.03/10,000操作 |
| 5 | **Supabase** | Free | 無料 | $0 | ¥0 | 500MB / pgvector |
| 6 | **Application Insights** | — | 従量 | ~$0 | ~¥0 | 5GB/月無料枠内 |
| | | | **合計** | **~$0.50-3.50** | **~¥75-525** | |

### 既存構成との比較

| | 既存構成 | Lite PoC | 削減率 |
|---|---------|----------|--------|
| 月額 | ~$90（¥13,500） | ~$0.50-3.50（¥75-525） | **96-99%** |
| 日額 | ~$3（¥450） | ~$0.02-0.12（¥3-18） | — |
| 固定費 | $87（AI Search + App Service） | $0 | **100%削減** |

コスト差の核心: **固定費がゼロに��った**。既存構成の $87/月（AI Search Basic $75 + App Service B1 $12）が全て従量課��に置き換わった。

### 本番構成（100-300名）月額コスト内訳

| # | リソース | SKU | 月額（USD） | 月額（JPY） |
|---|---------|-----|------------|-----------|
| 1 | Azure OpenAI (GPT-4o-mini) | Standard | ~$4 | ~¥600 |
| 2 | Azure OpenAI (embedding-small) | Standard | ~$0.10 | ~¥15 |
| 3 | Container Apps | Consumption | ~$5-10 | ~¥750-1,500 |
| 4 | Azure PostgreSQL Flexible | B1ms | ~$33 | ~¥5,000 |
| 5 | Key Vault | Standard | ~$0.05 | ~¥8 |
| 6 | Application Insights | — | ~$0-5 | ~¥0-750 |
| | | **合計** | **~$42-52** | **~¥6,300-7,900** |

---

## シート4: 作成手順

### 作成順序（依存順）

| 順 | リソース | 依存 | 作成時間目安 | 備考 |
|----|---------|------|------------|------|
| 1 | Resource Group | — | 1分 | |
| 2 | Supabase Project | — | 5分 | Azure 外。pgvector 拡張を有効化 |
| 3 | Key Vault + シークレット 3 件 | RG + #2 | 10分 | |
| 4 | Container Apps Environment | RG | 3分 | |
| 5 | Container App + EasyAuth | #3, #4 | 10分 | Entra ID SSO 設定含む |
| 6 | RBAC 設定（3件） | #5 | 5分 | |
| 7 | Application Insights | RG | 3分 | OpenTelemetry 接続文字列を Container App に設定 |
| | **合計** | | **約 40分** | 既存構成（約1.5時間）の半分以下 |

> Entra ID アプリ・Azure OpenAI は既存リソースを流用するため新規作成不要。

### PoC 終了後の削除

- Azure: リソースグループ削除で完了
- Supabase: プロジェクト削除（7日間アクセスなしで自動停止）

---

## 関連文書

| 文書 | 内容 |
|------|------|
| 要件定義書 | 前提条件・制約・コスト要件 |
| アーキテクチャ設計書 | コンポーネント構成・DB 設計 |
| セキュリティ設計書 | RBAC・シークレット詳細 |
| 既存リソース設計書 | [`04-resource-design.md`](https://github.com/YuhtaIhara/sharepoint-rag-azure/blob/main/docs/04-resource-design.md) |

---

以上
