# ACL 設計・構築ガイド

> ACL（アクセス制御）を正しく機能させるために必要な前提条件・手順・運用ガイド。

## 1. 前提: なぜ ACL に追加権限が必要か

SharePoint のフォルダ権限には以下の4種類のエントリが含まれる:

| 種類 | 例 | 必要な Graph API 権限 |
|------|------|---------------------|
| 個人ユーザー | user@example.com | `Files.Read.All` で取得可能 |
| M365 / セキュリティグループ | Sales Team | **`GroupMember.Read.All`** でメンバー展開 |
| ディレクトリロール | Global Administrator | **`Directory.Read.All`** でメンバー展開 |
| SP サイトメンバー | サイトに直接追加されたユーザー | **`Sites.Read.All`** でメンバー展開（未付与時は `SP_SITE_MEMBERS` 環境変数でフォールバック） |
| 共有リンク (anonymous/organization) | 「リンクを知っている全ユーザー」 | 権限不要で検出可能 |

個人ユーザーだけなら `Files.Read.All` + `Sites.Read.All` で足りるが、**グループやロールを個人メールに展開するには追加権限が必須**。これがないと `allowed_groups` に未解決のグループ ID が入り、ACL が機能しない。

> **`Sites.Read.All`** は権限エントリ取得だけでなく、SP サイトメンバー展開にも使用する。`Sites.Read.All` が付与できない環境では、`SP_SITE_MEMBERS` 環境変数（カンマ区切りメールアドレス）でフォールバック可能。

---

## 2. 必要な Graph API Application 権限（全5つ）

| 権限 | 用途 | appRoleId |
|------|------|-----------|
| `Files.Read.All` | SharePoint ファイル読み取り | `01d4889c-1287-42c6-ac1f-5d1e02578ef6` |
| `Sites.Read.All` | サイト・権限エントリ取得 | `332a536c-c7ef-4017-ab91-336970924f0d` |
| `GroupMember.Read.All` | グループメンバー展開 | `3afa6a7d-9b1a-42eb-948e-1650a849e176` |
| `Directory.Read.All` | ディレクトリロール展開 | `7ab1d382-f21e-4acd-a863-ba3e13f7da61` |
| `User.Read.All` | ユーザー情報・グループ所属取得 | `df021288-bdef-4463-88db-98f22de89214` |

### 付与手順

**方法A: Azure Portal**
1. Azure Portal → Entra ID → App registrations → 対象アプリ
2. API permissions → Add a permission → Microsoft Graph → Application permissions
3. 上記5つを追加
4. **Grant admin consent** を押す（Global Admin 必須）

**方法B: Azure CLI**
```bash
az ad app permission add --id $CLIENT_ID \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions \
    01d4889c-1287-42c6-ac1f-5d1e02578ef6=Role \
    332a536c-c7ef-4017-ab91-336970924f0d=Role \
    3afa6a7d-9b1a-42eb-948e-1650a849e176=Role \
    7ab1d382-f21e-4acd-a863-ba3e13f7da61=Role \
    df021288-bdef-4463-88db-98f22de89214=Role

az ad app permission admin-consent --id $CLIENT_ID
```

### 検証

```bash
TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&scope=https://graph.microsoft.com/.default&grant_type=client_credentials" \
  | jq -r .access_token)

# 全て 200 が返れば OK、403 なら権限不足
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/groups?\$top=1"
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/users?\$top=1"
```

インジェスト時に `check_graph_permissions()` が自動チェックするので、権限不足なら即座にエラー停止する。

---

## 3. SharePoint 匿名リンクの扱い

### 問題

SharePoint のフォルダに「リンクを知っているすべてのユーザー（anonymous scope）」の共有リンクがあると、ACL が `["*"]`（全員アクセス可）になる。

### 対処

| 方法 | 設定 | 動作 |
|------|------|------|
| **デフォルト（推奨）** | `REJECT_ANONYMOUS_LINKS=true` | 匿名リンクを無視し、他の権限（個人・グループ）だけで ACL を構成。匿名リンクの削除不要 |
| 補助: リンク削除 | SharePoint 管理画面で削除 | SharePoint 側のセキュリティリスクも解消（RAG とは別の問題） |
| 補助: テナント全体で禁止 | SP 管理センター → 共有ポリシー | 新規作成も防止 |

> **注意**: `REJECT_ANONYMOUS_LINKS=false` にすると、匿名リンクがあるフォルダは全ユーザーに公開される。制限フォルダに匿名リンクが残っている環境では危険。

### 確認方法（Graph API）

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/drives/$DRIVE_ID/root:/$FOLDER_NAME:/permissions" \
  | jq '.value[] | select(.link.scope=="anonymous")'
```

結果が空なら OK。

---

## 4. 対象フォルダの変更手順

### サイト・ドライブの特定

```bash
# サイト ID
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/sites/{hostname}:/{path}" | jq .id

# ドライブ ID
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/sites/$SITE_ID/drives" | jq '.value[] | {name, id}'
```

### フォルダ変更

1. `.env.local` の `SP_TARGET_FOLDERS` を編集（CSV 形式、前方一致）
2. フルインジェスト: `python -m src.ingest`
3. 旧フォルダのチャンクは自動削除される

---

## 5. 対応ドキュメント形式

| 形式 | 対応 | 備考 |
|------|------|------|
| `.pdf` | ○ | テキスト埋め込み PDF のみ。画像のみ PDF（OCR 必要）は未対応 |
| `.docx` | ○ | |
| `.xlsx` | ○ | 全シート・全セルをテキスト化 |
| `.pptx` | ○ | テキストフレーム内のみ |
| `.txt` / `.csv` / `.md` | ○ | UTF-8 + chardet 自動検出 |
| `.doc` / `.xls` / `.ppt` | × | 旧 Office 形式。スキップレポートに出力 |
| 画像 / 動画 / 音声 | × | テキストデータではないため対象外 |
| パスワード付き | × | スキップレポートに出力 |
| DRM 付き | × | 法的・技術的に対応不可 |

未対応ファイルは**スキップレポート**で確認可能（インジェスト完了時にログ出力）。
