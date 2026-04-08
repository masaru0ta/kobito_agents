# Phase 8: サーバーログ仕様

## 目的

サーバーログを「意味のある情報だけを、分かりやすい形式で出力する」ことを目標とする。
ノイズを排除し、何が起きているかをログだけで把握できる状態にする。

---

## ログ出力フォーマット

### 通常ログ

```
YYYY-MM-DD HH:MM:SS [module.name] メッセージ
```

モジュール名はPythonのパッケージ構造に基づく（例：`server.cli_bridge` = `src/server/cli_bridge.py`）。

### アクセスログ

```
YYYY-MM-DD HH:MM:SS METHOD /path/to/endpoint STATUS
```

例：
```
2026-04-08 11:31:27 GET /api/agents 200
2026-04-08 11:31:28 POST /api/agents/system/chat 200
```

IP・ポート番号・プロトコルバージョン（HTTP/1.1）は出力しない。

---

## カラーリング

### 通常ログ

| 対象 | 色 |
|------|-----|
| タイムスタンプ | 薄いグレー（dim） |
| `[server.cli_bridge]` | シアン |
| `[server.scheduler]` | グリーン |
| WARNING レベル | 黄色 |
| ERROR レベル | 赤 |

### チャットログ（モジュール名なし）

| ラベル | 色 |
|--------|-----|
| `チャット受信` | 太字グリーン |
| `チャンク受信` | 薄いグレー（dim） |
| `ツール実行` | 薄いグレー（dim） |
| エージェント名 | シアン |
| メッセージ内容 | 黄色 |
| ツール名 | マゼンタ |

### アクセスログ

| 対象 | 色 |
|------|-----|
| 2xx ステータス | 薄いグレー（dim） |
| 4xx ステータス | 黄色 |
| 5xx ステータス | 赤 |

---

## フィルタリング（出力しないログ）

以下は意味のないノイズとして抑制する。

| 対象 | 理由 |
|------|------|
| 静的ファイル（`.css` `.js` `.ico` `.png` 等） | UIリソースの配信は情報価値なし |
| `GET /api/.../process-status` | 5秒ポーリング |
| `GET /api/.../tasks` | 5秒ポーリング |
| `GET /api/scheduler/status` | 30秒ポーリング |
| `run_stream終了`（DEBUG降格） | チャット返信ログで代替 |

---

## チャットログ仕様

チャット処理の流れをログで追跡できるようにする。

### チャット受信

ユーザーメッセージを受け取った時点で出力。

```
チャット受信 agent=<エージェント名> 「<メッセージ冒頭20文字>」
```

### チャンク受信

Claudeからのレスポンストークンが届くたびに出力。

```
チャンク受信 agent=<エージェント名> 「<チャンク冒頭20文字>」 sid=<session_id冒頭8文字>
```

`sid` は `body.session_id` を使用。新規セッションの場合は `new` と表示される。

### ツール実行

Claudeがツールを呼び出したときに出力。

```
ツール実行 agent=<エージェント名> <ツール名>[: <対象>] sid=<session_id冒頭8文字>
```

例：
```
ツール実行 agent=レプリカ Bash: ls sid=5a3edfa6
ツール実行 agent=レプリカ Read: app.py sid=5a3edfa6
```

---

## プロセスライフサイクルログ

Claude CLIプロセスの状態変化を出力する（`server.cli_bridge`）。

| ログメッセージ | タイミング |
|----------------|-----------|
| `プロセス起動: <path>::<session_id> (model=<model>)` | CLIプロセス生成時 |
| `アイドルタイムアウトによりプロセス終了: <key>` | 一定時間未使用で自動終了 |
| `プロセス異常終了を検出: <key>` | 次回リクエスト時にプロセスの死亡を検知 |
| `モデル変更検出 (<old> → <new>): プロセス再起動` | モデル変更によるプロセス再起動 |

---

## サーバー起動・終了ログ

```
2026-04-08 11:31:23 [server.app] サーバー起動 port=8200
2026-04-08 11:31:23 [server.scheduler] スケジューラー タイマーループ開始
2026-04-08 11:31:23 [uvicorn.error] Application startup complete.
2026-04-08 11:31:23 [uvicorn.error] Uvicorn running on http://0.0.0.0:8200
```

uvicornの最初の2行（`Started server process`、`Waiting for application startup`）は
lifespanより前に出力されるため旧フォーマットのまま残る。

---

## 実装ファイル

| ファイル | 内容 |
|----------|------|
| `src/server/app.py` | ロギング初期化・フォーマッター・フィルター定義 |
| `src/server/routes/chat.py` | チャット受信・チャンク受信・ツール実行ログ |
| `src/server/cli_bridge.py` | プロセスライフサイクルログ |
| `src/server/scheduler.py` | スケジューラーログ |
