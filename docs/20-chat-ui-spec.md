# チャット UI リライト設計書

## 概要

現行ダークテーマの `index.html` を Pattern B（ライトモード・ブルーアクセント・サイドバー付き）で全面リライトする。

## デザイン方針

- モック: `src/static/mock-chat-v2b.html` を正本とする
- ライトモード・ブルーアクセント（`#339af0`）
- ChatGPT 風レイアウト: 左サイドバー + 右メインエリア
- モバイル（768px 以下）: サイドバー非表示

## 機能一覧

### F1: サイドバー（チャット履歴）
- 過去の会話セッション一覧を Today / Yesterday / Last 7 days でグループ表示
- クリックで過去の会話を読み込み
- ヘッダーの「+」ボタンで新しいチャット開始
- **新規 API 必要**: `GET /v1/conversations`

### F2: SSE ストリーミング表示
- `/v1/chat/stream` を使用してリアルタイムにトークン表示
- ストリーミング中はカーソル点滅アニメーション表示
- `done` イベントで出典を表示

### F3: Markdown レンダリング
- `**太字**` → `<strong>`
- `[N]` → クリック可能なインライン出典番号
- リスト（`- item`）→ `<ul><li>`
- 段落分割（`\n\n`）

### F4: フィードバック（👍👎）
- AI メッセージにホバーで表示されるアクションボタン
- `/v1/feedback` API に連携（rating: 1 or -1）
- message_id は会話の conversation ID を使用

### F5: インライン出典
- AI 回答中の `[1]` をクリック可能なバッジとして表示
- 回答下部に出典カード一覧（タイトル＋リンク）

### F6: ウェルカム画面
- 初期状態でロゴ・説明・サジェスチョンボタン表示
- サジェスチョンクリックで質問送信

## 追加 API

### GET /v1/conversations
- 認証ユーザーの過去セッション一覧を返す
- レスポンス:
```json
[
  {
    "session_id": "uuid",
    "title": "最初のメッセージ冒頭30文字",
    "created_at": "2026-03-27T10:00:00Z",
    "message_count": 4
  }
]
```
- 最新 50 件、降順
- conversations テーブルから `session_id` ごとに集約

### GET /v1/conversations/{session_id}/messages
- 特定セッションのメッセージ履歴を返す
- レスポンス:
```json
[
  {
    "id": "uuid",
    "role": "user",
    "content": "質問文",
    "citations": null,
    "created_at": "2026-03-27T10:00:00Z"
  }
]
```

## SSE イベント仕様（既存）

```
data: {"type":"chunk","content":"テキスト断片"}\n\n
data: {"type":"done","citations":[...],"session_id":"..."}\n\n
data: {"type":"error","message":"エラーメッセージ"}\n\n
```

## フィードバック API（既存）

```
POST /v1/feedback
{
  "session_id": "...",
  "message_id": "...",
  "rating": 1,       // 1=thumbs up, -1=thumbs down
  "comment": ""
}
```

## 実装計画

1. **バックエンド**: `GET /v1/conversations` + `GET /v1/conversations/{session_id}/messages` を追加
2. **フロントエンド**: `index.html` を mock-chat-v2b.html ベースで全面リライト
   - SSE ストリーミング接続
   - サイドバー + 会話履歴
   - Markdown レンダリング
   - フィードバックボタン
3. **テスト**: 新 API のユニットテスト追加
4. **ローカル動作確認** → デプロイ
