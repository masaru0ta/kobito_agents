# Phase 1 仕様書: Web UIチャット + AI設定

## 概要

Web UIからエージェント（= Claude Codeプロジェクト）とチャットし、AI設定（CLAUDE.md）を編集できる。
kobito_agents自身がシステム管理エージェントとして初期登録済み。
CLIとWebUIで同じ会話を継続できる。

モックアップ: [mockup_phase1.html](mockup_phase1.html)

## 設計原則

- **CLIツールのネイティブデータを直接読む** — 会話データを二重管理しない
- **アダプターパターン** — Claude Code / Codex等、CLIツールごとに読み取りを切り替える
- **CLIとWebUIの会話継続** — WebUIで始めた会話をCLIで、CLIで始めた会話をWebUIで続けられる

## データモデル

### エージェント登録情報（`data/agents.json`）

```json
[
  {
    "id": "system",
    "name": "レプリカ",
    "path": "D:/AI/code/kobito_agents",
    "description": "システム管理エージェント",
    "cli": "claude",
    "model_tier": "deep"
  }
]
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|-----|-----------|------|
| id | string | — | 一意識別子 |
| name | string | — | 表示名 |
| path | string | — | プロジェクトディレクトリの絶対パス |
| description | string | "" | 説明 |
| cli | string | "claude" | CLIツール種別（"claude" / "codex"） |
| model_tier | string | "deep" | モデルティア |

#### モデルティア

| model_tier | 性質 | Claude | Codex |
|-----------|------|--------|-------|
| `"deep"` | 遅いが賢い | opus | o3 |
| `"quick"` | 速くて軽い | sonnet | o4-mini |

### 会話データ

CLIツールのネイティブストレージを直接読む。kobito_agents側にコピーは持たない。

| CLIツール | 保存先 |
|----------|--------|
| Claude Code | `~/.claude/projects/{project_hash}/{session_id}.jsonl` |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`（将来対応） |

### メタデータ（`{project_dir}/.kobito/meta/{session_id}.json`）

kobito_agents固有のメタデータをキャッシュする。

- title/summary（要約）
- hidden（非表示フラグ — 削除ボタンで立てる。ネイティブJSONLは消さない）

## コンポーネント

| コンポーネント | ファイル | 役割 |
|--------------|---------|------|
| ConfigManager | `config.py` | エージェント登録情報の管理。起動時にsystemエージェントを自動登録 |
| SessionReader | `session_reader.py` | CLIツールのセッションデータ読み取り。アダプターパターン |
| CLIBridge | `cli_bridge.py` | CLIツールを呼び出してプロンプトを送り、ストリーミングで応答を返す |
| ChatManager | `chat.py` | セッション一覧・履歴取得、メッセージ送信、要約生成を統合 |

## Web API

#### エージェント
- `GET /api/agents` — 一覧
- `GET /api/agents/{id}` — 詳細
- `PUT /api/agents/{id}` — 設定更新（name, description）
- `GET /api/agents/{id}/system-prompt` — CLAUDE.md取得
- `PUT /api/agents/{id}/system-prompt` — CLAUDE.md更新

#### チャット
- `GET /api/agents/{id}/sessions` — セッション一覧
- `GET /api/agents/{id}/sessions/{session_id}` — セッション履歴
- `POST /api/agents/{id}/sessions/{session_id}/summarize` — 会話要約
- `POST /api/agents/{id}/chat` — メッセージ送信（SSEストリーミング）

#### CLI
- `POST /api/agents/{id}/cli` — CLIをターミナルで起動

## Web UI

モックアップ参照。3ペイン構成。

| ペイン | 幅 | 内容 |
|-------|-----|------|
| 左 | 220px固定 | エージェント一覧 |
| 中央 | 320px（200〜600pxリサイズ可、localStorage保存） | 会話タブ: セッションリスト（応答待ちインジケーター付き） / 設定タブ: name, desc, model_tier, path |
| 右 | 残り（最小300px） | 会話タブ: チャット画面 / 設定タブ: CLAUDE.md編集 |

## サーバー

- FastAPI + uvicorn、ポート8300、ホットリロード
- `start.bat` で起動（`uvicorn server.app:app --port 8300 --reload` + ブラウザ自動起動）

## ディレクトリ構成

```
kobito_agents/
  CLAUDE.md
  start.bat
  docs/
    spec_phase1.md
    mockup_phase1.html
  data/
    agents.json
  src/
    server/
      app.py
      config.py
      session_reader.py
      cli_bridge.py
      chat.py
      routes/
        agents.py
        chat.py
        deps.py
      static/
        index.html
        app.js
        style.css
```

## テスト項目

### ConfigManager
- [ ] 起動時にsystemエージェントが自動登録される
- [ ] agents.jsonが存在しない場合、systemエージェントを含むファイルが作成される
- [ ] エージェント一覧が取得できる
- [ ] エージェント詳細が取得できる（CLAUDE.mdの内容を含む）
- [ ] 存在しないエージェントIDでエラーが返る
- [ ] name, descriptionを更新できる
- [ ] CLAUDE.mdを読み取れる
- [ ] CLAUDE.mdを更新でき、ファイルに反映される
- [ ] model_tierを更新できる

### SessionReader（ClaudeSessionReader）
- [ ] プロジェクトパスからproject_hashを正しく算出できる（`\` と `:` を `-` に置換）
- [ ] `~/.claude/projects/{hash}/` 配下のJSONLファイル一覧からセッション一覧を取得できる
- [ ] セッション一覧が新しい順にソートされている
- [ ] 各セッションの日時、メッセージ件数、最終メッセージプレビューが取得できる
- [ ] 指定session_idのJSONLからuser/assistantメッセージを抽出できる
- [ ] assistantメッセージ内のtool_use情報を抽出できる
- [ ] 非表示フラグが立っているセッションが一覧から除外される

### CLIBridge
- [ ] `claude -p` でプロンプトを送信し、ストリーミングで応答を受け取れる
- [ ] cwdがエージェントのプロジェクトパスに設定される
- [ ] session_id指定時に `--resume` で会話を継続できる
- [ ] model_tierに応じた正しいモデル名が使用される
- [ ] stream-json出力からテキストチャンクとtool_use通知を分離できる
- [ ] 実行結果のsession_idを取得できる

### ChatManager
- [ ] メッセージ送信でCLIBridgeが呼ばれ、ストリーミングで応答が返る
- [ ] セッション一覧がSessionReader経由で取得できる
- [ ] セッション履歴がSessionReader経由で取得できる
- [ ] セッションを非表示にできる（`.kobito/meta/{session_id}.json` にhiddenフラグ）
- [ ] 要約を生成し、`.kobito/meta/{session_id}.json` に保存できる

### Web API
- [ ] `GET /api/agents` でエージェント一覧が返る
- [ ] `GET /api/agents/{id}` でエージェント詳細が返る
- [ ] `PUT /api/agents/{id}` でname, description, model_tierが更新できる
- [ ] `GET /api/agents/{id}/system-prompt` でCLAUDE.mdが返る
- [ ] `PUT /api/agents/{id}/system-prompt` でCLAUDE.mdが更新できる
- [ ] `GET /api/agents/{id}/sessions` でセッション一覧が返る
- [ ] `GET /api/agents/{id}/sessions/{session_id}` でセッション履歴が返る
- [ ] `POST /api/agents/{id}/chat` でSSEストリーミング応答が返る
- [ ] `POST /api/agents/{id}/sessions/{session_id}/summarize` で要約が返る
- [ ] `POST /api/agents/{id}/cli` でターミナルが起動する
- [ ] 存在しないエージェントIDで404が返る

### Web UI
- [ ] 左ペインにエージェント一覧が表示される
- [ ] エージェント選択でセッション一覧が切り替わる
- [ ] セッション一覧に日時、件数(N)、プレビューが表示される
- [ ] セッション選択でチャット履歴が右ペインに表示される
- [ ] 新規会話ボタンで新しいセッションが開始される
- [ ] メッセージ送信でストリーミング応答が表示される
- [ ] ツール使用通知が表示される
- [ ] 「要約する」ボタンで要約が生成される
- [ ] 「CLI起動」ボタンでターミナルが開く
- [ ] 「非表示」ボタンでセッションが一覧から消える
- [ ] 設定タブでname, description, model_tierが編集・保存できる
- [ ] 設定タブでCLAUDE.mdが編集・保存できる
- [ ] 中央・右ペインのリサイズが機能する
- [ ] リサイズ幅がリロード後も維持される
- [ ] Enter送信、Shift+Enter改行が機能する
- [ ] 複数セッションで同時に会話できる
- [ ] 応答待ちセッションのインジケーターがセッション一覧に表示される
- [ ] 応答待ちセッションから離れて戻った時、状態が正しく復元される

## Phase 1でやらないこと

- 外部プロジェクトの登録・削除API
- CodexSessionReader
- トリガー・定期実行
- エージェント間通信
- 記憶
- 自律思考
