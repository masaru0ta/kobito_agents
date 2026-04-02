# Phase 1 仕様書: Web UIチャット + AI設定

## 概要

Web UIからエージェント（= Claude Codeプロジェクト）とチャットし、AI設定（CLAUDE.md）を編集できる。
kobito_agents自身がシステム管理エージェントとして初期登録済み。
CLIとWebUIで同じ会話を継続できる。CLIでの操作はWebUIに5秒以内に自動反映される。

モックアップ: [mockup_phase1.html](mockup_phase1.html)

## 設計原則

- **CLIツールのネイティブデータを直接読む** — 会話データを二重管理しない
- **アダプターパターン** — Claude Code / Codex等、CLIツールごとに読み取りを切り替える
- **CLIとWebUIの会話継続** — WebUIで始めた会話をCLIで、CLIで始めた会話をWebUIで続けられる
- **常駐プロセス方式** — セッションごとにCLIプロセスを保持し、毎回の履歴送信コストを削減する
- **ノイズ除去** — CLIツールが挿入するメタデータ（コンテキスト継続サマリー等）を表示から除外する

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
| model_tier | string | "deep" | エージェントのデフォルトモデルティア |

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

kobito_agents固有のメタデータをプロジェクトディレクトリに保存する。

| フィールド | 説明 |
|-----------|------|
| title | ユーザーが任意に設定する会話タイトル。セッション一覧・チャットヘッダーに表示 |
| hidden | 非表示フラグ — 非表示ボタンで立てる。ネイティブJSONLは消さない |
| model_tier | セッションごとのモデルティア設定。エージェントデフォルトを上書き |

## コンポーネント

| コンポーネント | ファイル | 役割 |
|--------------|---------|------|
| ConfigManager | `config.py` | エージェント登録情報の管理。起動時にsystemエージェントを自動登録 |
| SessionReader | `session_reader.py` | CLIツールのセッションデータ読み取り。アダプターパターン |
| CLIBridge | `cli_bridge.py` | 常駐プロセスプールを管理し、セッションごとのclaude--input-format stream-jsonプロセスを保持する |

## CLIBridge 常駐プロセス方式

セッションごとに `claude --input-format stream-json --output-format stream-json` プロセスを起動し、プールに保持する。

- メッセージ送信: stdinにNDJSON（`{"type":"user","message":{"role":"user","content":"..."}}`）を書き込む
- 応答受信: stdoutのJSONLをキューに流し、`result` イベントまでをストリーミングで返す
- 同一セッションへの同時送信はロックで排他制御する
- アイドルタイムアウト: 10分間送信がなければプロセスをKILL
- モデル変更検出: 指定モデルが変わった場合、既存プロセスをKILLして再起動
- サーバー停止時: 全プロセスを終了する（lifespan + atexit）

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
- `PUT /api/agents/{id}/sessions/{session_id}/title` — タイトル更新
- `PUT /api/agents/{id}/sessions/{session_id}/model-tier` — セッションのモデルティア更新
- `POST /api/agents/{id}/sessions/{session_id}/hide` — 非表示フラグを立てる
- `POST /api/agents/{id}/chat` — メッセージ送信（SSEストリーミング）
- `GET /api/agents/{id}/process-status?watching={session_id}` — 稼働プロセス一覧 + 更新検知情報

#### CLI
- `POST /api/agents/{id}/cli` — CLIをターミナルで起動

### SSEイベント仕様（`POST /api/agents/{id}/chat`）

| イベント | データ | 説明 |
|---------|--------|------|
| `chunk` | `{type:"chunk", data:"累積テキスト"}` | 応答テキストの累積チャンク |
| `tool_use` | `{type:"tool_use", data:"ツール名: 対象"}` | ツール使用通知 |
| `session_id` | `{type:"session_id", data:"セッションID"}` | セッションID確定（新規時） |
| `done` | `{type:"done"}` | 完了 |
| `error` | `{type:"error", data:"エラーメッセージ"}` | プロセス異常終了等のエラー |
| `: ping` | — | 15秒間隔のキープアライブ（接続維持） |

### `GET /api/agents/{id}/process-status` レスポンス

```json
{
  "startup_id": "uuid",
  "active": ["session_id1", "session_id2"],
  "responding": ["session_id1"],
  "dir_mtime": 1234567890.123,
  "watching_mtime": 1234567890.456
}
```

| フィールド | 説明 |
|-----------|------|
| startup_id | サーバー起動ごとに生成されるUUID。変化でサーバー再起動を検知 |
| active | 稼働中プロセスのセッションID一覧（緑ドット表示に使用） |
| responding | 現在ロック取得中（応答処理中）のセッションID一覧 |
| dir_mtime | セッションJSONL群の最大更新時刻（一覧変化の検知に使用） |
| watching_mtime | `watching` パラメータで指定したセッションのJSONL更新時刻 |

## Web UI

モックアップ参照。3ペイン構成。

| ペイン | 幅 | 内容 |
|-------|-----|------|
| 左 | 220px固定 | エージェント一覧 |
| 中央 | 320px（200〜600pxリサイズ可、localStorage保存） | 会話タブ: セッションリスト / 設定タブ: name, desc, model_tier, path |
| 右 | 残り（最小300px） | 会話タブ: チャット画面 / 設定タブ: CLAUDE.md編集 |

### レスポンシブ対応（768px以下）

- 左ペイン・リサイズハンドル非表示、1カラム表示に切替
- セッション一覧をデフォルト表示。セッション選択でチャット画面にスライド切替
- チャットヘッダー左端の `←` ボタンでセッション一覧に戻る
- 設定画面でも `←` ボタンで会話タブに戻れる
- `viewport-fit=cover` + `env(safe-area-inset-*)` でノッチ・ホームインジケーター対応
- ヘッダーを1行に収めるコンパクトレイアウト

### セッション一覧

- 日時・メッセージ数（件数）・タイトル・プレビューを表示
- **稼働中プロセスの緑ドット表示** — 常駐プロセスが生きているセッションに緑点灯
- **応答待ちインジケーター** — 応答中のセッションにスピナー＋「応答待ち」表示。フロントとバックエンドの二重チェックで消え忘れ・見逃しを防止
- **非表示ボタン** — 確認ダイアログを経てからセッションを一覧から除外（JSOLは残す）
- **5秒ポーリングによる自動更新** — CLIやその他外部操作でセッションが更新されると自動反映
- **サーバー再起動検知** — `startup_id` の変化を検知し、上部トースト通知＋セッション一覧・履歴を自動再取得

### チャット画面

- ヘッダー: タイトル（クリックで編集）＋モデル選択（Sonnet/Opus、セッションごとに永続化）
- 応答はストリーミングで逐次表示。完了時にMarkdownレンダリング
- **応答待ちインジケーター** — 「応答待ち」スピナーが完了まで表示され続ける
- ツール使用通知をインラインで表示
- Markdownの見出し・強調・コード・リストを色分け表示
- **送信方法**: Enter送信 or Shift+Enter送信（切替可能）。IME確定Enterは誤送信しない

### リアルタイム同期（5秒ポーリング）

`GET /api/agents/{id}/process-status` を5秒ごとに呼び出し、以下を検知する：

1. **プロセス稼働状態** → 緑ドット表示を更新
2. **`dir_mtime` 変化** → セッション一覧を再取得（CLIで新規セッション作成・既存セッション更新を検知）
3. **`watching_mtime` 変化** → 表示中セッションの履歴を再取得（CLIでの書き込みをリアルタイム反映）
   - 自分がストリーミング送信中の場合はスキップ（SSEで受け取っているため）

## サーバー

- FastAPI + uvicorn、ポート8200、ホットリロード
- `start.bat` で起動（`uvicorn server.app:app --host 0.0.0.0 --port 8200 --reload` + ブラウザ自動起動）
- `0.0.0.0` バインドにより LAN 上の全デバイスからアクセス可能（VPN経由も含む）

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
- [ ] エージェント詳細が取得できる
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
- [ ] コンテキスト継続サマリー（"This session is being continued..."）がフィルタリングされる
- [ ] assistantメッセージ内のtool_use情報を抽出できる
- [ ] 非表示フラグが立っているセッションが一覧から除外される
- [ ] get_dir_mtime がセッションJSONL群の最大mtimeを返す
- [ ] get_session_mtime が指定セッションのJSONLのmtimeを返す

### CLIBridge（常駐プロセス方式）
- [ ] セッションIDに対応するプロセスがプールに作成される
- [ ] 同一セッションへの2回目以降のメッセージ送信で既存プロセスが再利用される
- [ ] model変更時に既存プロセスをKILLして新規プロセスを起動する
- [ ] 同一セッションへの同時送信がロックで排他制御される
- [ ] アイドル10分でプロセスがKILLされる
- [ ] プロセス異常終了を検知してプールから除去する
- [ ] active_session_ids が稼働中のセッションIDを返す
- [ ] サーバー停止時に全プロセスが終了する

### Web API
- [ ] `GET /api/agents` でエージェント一覧が返る
- [ ] `GET /api/agents/{id}` でエージェント詳細が返る
- [ ] `PUT /api/agents/{id}` でname, description, model_tierが更新できる
- [ ] `GET /api/agents/{id}/system-prompt` でCLAUDE.mdが返る
- [ ] `PUT /api/agents/{id}/system-prompt` でCLAUDE.mdが更新できる
- [ ] `GET /api/agents/{id}/sessions` でセッション一覧が返る
- [ ] `GET /api/agents/{id}/sessions/{session_id}` でセッション履歴が返る
- [ ] `PUT /api/agents/{id}/sessions/{session_id}/title` でタイトルが更新できる
- [ ] `PUT /api/agents/{id}/sessions/{session_id}/model-tier` でモデルティアが更新できる
- [ ] `POST /api/agents/{id}/sessions/{session_id}/hide` で非表示フラグが立つ
- [ ] `POST /api/agents/{id}/chat` でSSEストリーミング応答が返る
- [ ] `POST /api/agents/{id}/chat` でプロセス異常終了時にerrorイベントが返る
- [ ] `GET /api/agents/{id}/process-status` で稼働プロセス一覧とmtimeが返る
- [ ] `POST /api/agents/{id}/cli` でターミナルが起動する
- [ ] 存在しないエージェントIDで404が返る

### Web UI
- [ ] 左ペインにエージェント一覧が表示される
- [ ] エージェント選択でセッション一覧が切り替わる
- [ ] セッション一覧に日時、件数(N)、タイトル、プレビューが表示される
- [ ] 稼働中プロセスのセッションに緑ドットが表示される
- [ ] 応答中のセッションに「応答待ち」スピナーが表示される（完了まで）
- [ ] チャットヘッダーのタイトルクリックでタイトルを編集できる
- [ ] チャットヘッダーのモデル選択でSonnet/Opusを切り替えられる
- [ ] モデル選択がリロード後も維持される（セッションごとにメタデータ保存）
- [ ] セッション選択でチャット履歴が右ペインに表示される
- [ ] 新規会話ボタンで新しいセッションが開始される
- [ ] メッセージ送信でストリーミング応答が表示される（Markdown対応）
- [ ] ツール使用通知が表示される
- [ ] 「CLI起動」ボタンでターミナルが開く
- [ ] 「非表示」ボタンで確認ダイアログが表示され、承認でセッションが一覧から消える
- [ ] 設定タブでname, description, model_tierが編集・保存できる
- [ ] 設定タブでCLAUDE.mdが編集・保存できる
- [ ] 中央・右ペインのリサイズが機能する
- [ ] リサイズ幅がリロード後も維持される
- [ ] Enter送信、Shift+Enter改行が機能する（IME確定Enterでは誤送信しない）
- [ ] 複数セッションで同時に会話できる
- [ ] 5秒ポーリングでCLI操作がWebUIに自動反映される
- [ ] 別セッション表示中もセッション一覧が自動更新される

## Phase 1でやらないこと

- 外部プロジェクトの登録・削除API
- CodexSessionReader
- トリガー・定期実行
- エージェント間通信
- 記憶
- 自律思考
