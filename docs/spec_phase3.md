# Phase 3 仕様書: スケジューラーエンジン

## 概要

`task_order.json` の先頭タスクを自動的に1ステップずつ実行するスケジューラーエンジンを実装する。
人間がWeb UIからON/OFFを制御し、承認済みタスクのみが実行対象となる。

## 設計原則

- **人間がスイッチを握る** — スケジューラーのON/OFFは人間のみが操作する。サーバー再起動時はOFFで安全側に倒す
- **二重実行の構造的防止** — 実行中フラグにより、前回のセッションが完了するまで次の実行を抑止する
- **既存インフラの再利用** — タスクコンテキスト注入（Phase 2）、CLIBridge常駐プロセス（Phase 1）をそのまま活用する

## スケジューラーエンジン

### 基本動作

| 項目 | 値 |
|------|-----|
| 実行間隔 | 10分 |
| 二重実行防止 | 実行中フラグ（開始時ON、SSEの `done` イベントでOFF） |
| ON/OFF制御 | Web UIトグル。サーバー起動時はOFF |
| 状態永続化 | なし（メモリ保持。サーバー再起動でOFFに戻る） |

### 実行ロジック

10分ごとに以下のフローが発火する。

```
スケジューラー発火
  ↓
OFFなら終了
  ↓
実行中フラグがONなら終了
  ↓
task_order.json を先頭から走査
  ↓
各タスクを評価:
  - メタデータの approval が approved でなければスキップ
  - phase が done ならスキップ
  - 上記を通過した最初のタスクを実行対象とする
  ↓
対象なし → 終了
  ↓
対象あり → 実行中フラグON → 新規作業セッションを開始
  - 既存のタスクコンテキスト注入（task_mode: work）を使用
  - セッションIDをメタデータの sessions[] に追記
  - 完了時（done イベント）→ 実行中フラグOFF
```

### タスク選定の詳細

`task_order.json` の配列を先頭から順に走査し、以下の条件をすべて満たす最初のタスクを実行対象とする。

| 条件 | チェック対象 |
|------|------------|
| 承認済み | メタデータの `approval === "approved"` |
| 未完了 | frontmatterの `phase !== "done"` |

条件を満たすタスクが存在しない場合、そのサイクルでは何も実行しない。

## コンポーネント

| コンポーネント | ファイル | 役割 |
|--------------|---------|------|
| Scheduler | `src/server/scheduler.py` | タイマー管理、実行判定、タスク選定、セッション開始 |

### Scheduler

- `asyncio` ベースのタイマーループ
- FastAPIの `lifespan` で起動・停止を管理
- ON/OFF状態・実行中フラグは `Scheduler` インスタンスの属性で管理

#### 内部状態

| 属性 | 型 | 初期値 | 説明 |
|------|-----|--------|------|
| `enabled` | bool | `False` | ON/OFF状態 |
| `running` | bool | `False` | 実行中フラグ |
| `last_run` | datetime\|None | `None` | 最後の実行時刻 |
| `next_run` | datetime\|None | `None` | 次回の予定実行時刻 |

#### ライフサイクル

```
サーバー起動（lifespan）
  ↓
Scheduler インスタンス生成
  ↓
タイマーループ開始（asyncio.create_task）
  ↓
10分ごとに実行ロジックを発火
  ↓
サーバー停止（lifespan）
  ↓
タイマーループをキャンセル
```

## Web API（追加分）

| エンドポイント | メソッド | 動作 |
|--------------|---------|------|
| `/api/scheduler/status` | GET | スケジューラーの現在状態を返す |
| `/api/scheduler/toggle` | POST | ON/OFF切り替え |

### `GET /api/scheduler/status` レスポンス

```json
{
  "enabled": false,
  "running": false,
  "last_run": null,
  "next_run": null
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| enabled | bool | ON/OFF状態 |
| running | bool | 実行中フラグ |
| last_run | string\|null | 最後の実行時刻（ISO8601） |
| next_run | string\|null | 次回の予定実行時刻（ISO8601）。OFFの場合はnull |

### `POST /api/scheduler/toggle` レスポンス

レスポンスは `GET /api/scheduler/status` と同形式。トグル後の状態を返す。

## Web UI（追加分）

### サイドバー下部

```
[再起動]
[● スケジューラー ON] ← トグルボタン
```

- ONのとき緑インジケーター + 「ON」、OFFのとき灰色 + 「OFF」
- クリックで `POST /api/scheduler/toggle` を呼び出し
- 5秒ポーリングで状態を同期（`GET /api/scheduler/status`）

## ディレクトリ構成（追加分）

```
kobito_agents/
  src/
    server/
      scheduler.py          ← 追加
      routes/
        scheduler.py        ← 追加
```

## テスト項目

### Scheduler

- [ ] サーバー起動時にスケジューラーがOFF状態で初期化される
- [ ] トグルでON/OFFが切り替わる
- [ ] OFF状態ではタスクが実行されない
- [ ] 実行中フラグがONの間は次の実行がスキップされる
- [ ] `task_order.json` の先頭から順にタスクを評価する
- [ ] `approval` が `approved` でないタスクはスキップされる
- [ ] `phase` が `done` のタスクはスキップされる
- [ ] 条件を満たすタスクがない場合、何も実行しない
- [ ] 対象タスク発見時に新規作業セッションが開始される
- [ ] セッション開始時にタスクコンテキスト（task_mode: work）が注入される
- [ ] セッションIDがメタデータの `sessions[]` に追記される
- [ ] SSEの `done` イベントで実行中フラグがOFFになる
- [ ] `last_run` が実行ごとに更新される
- [ ] `next_run` がON状態で正しく算出される
- [ ] サーバー停止時にタイマーループが正常終了する

### Web API

- [ ] `GET /api/scheduler/status` でスケジューラー状態が返る
- [ ] `POST /api/scheduler/toggle` でON/OFFが切り替わり、切り替え後の状態が返る

### Web UI

- [ ] サイドバー下部にスケジューラートグルボタンが表示される
- [ ] ONのとき緑インジケーター + 「ON」が表示される
- [ ] OFFのとき灰色インジケーター + 「OFF」が表示される
- [ ] クリックでON/OFFが切り替わる
- [ ] 5秒ポーリングで状態が自動同期される

## Phase 3でやらないこと

- 定期タスク（`schedule` フィールドによる `done → draft` リセット）
- エージェント間通信
- 自律思考（タスクなしでの自発行動）
- 記憶
