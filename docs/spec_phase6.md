# Phase 6 仕様書: AI間通信（MCP ask_agent）

## 概要

エージェントが作業中に他のエージェントへ質問し、回答を得られる仕組みを実装する。
kobito が MCP サーバーを公開し、各エージェントの Claude Code セッションに `ask_agent` ツールを提供する。
内部では既存の CLIBridge を使って相手エージェントのセッションを立て、回答を返す。

---

## 1. 全体構成

```
Agent A (Claude Code セッション)
  ├── 標準ツール (Read, Write, Bash, etc.)
  └── MCP: kobito サーバー
       └── ask_agent(agent_id, message, session_id?)
                ↓
       kobito FastAPI サーバー
                ↓
       POST /api/internal/ask
                ↓
       CLIBridge → Agent B の新規/既存セッション
                   （新規セッション時: agent_communication.md をシステムプロンプトとして注入）
                ↓
       回答 + session_id を返却
                ↓
       Agent A のツール結果として表示
```

---

## 2. MCP ツール定義

### `ask_agent`

他のエージェントにメッセージを送り、回答を得る。

#### パラメータ

| 名前 | 型 | 必須 | 説明 |
|------|-----|------|------|
| `agent_id` | string | Yes | 送信先エージェントのID |
| `message` | string | Yes | 送信するメッセージ |
| `session_id` | string | No | 既存セッションを継続する場合に指定 |

#### 戻り値

```json
{
  "agent_id": "agent_20260404_141316_3af",
  "agent_name": "コード管理君",
  "session_id": "abc-123-def",
  "response": "調査した結果、該当するリポジトリは3件です..."
}
```

#### 使用例

```
// 新規会話
ask_agent(agent_id="agent_B", message="このAPI設計どう思う？")
→ { agent_id: "agent_B", session_id: "abc123", response: "..." }

// 続きの会話（同じセッション）
ask_agent(agent_id="agent_B", session_id="abc123", message="じゃあこう直したら？")
→ { agent_id: "agent_B", session_id: "abc123", response: "..." }
```

---

## 3. MCP サーバー実装

### 方式

stdio ベースの MCP サーバー（Python スクリプト）。
Claude Code がプロセスとして起動し、stdin/stdout で通信する。

### ファイル配置

```
src/mcp_server/
  ask_agent.py    # MCP サーバー本体
```

### 動作

1. Claude Code が `ask_agent.py` を子プロセスとして起動
2. Agent A が `ask_agent` ツールを呼び出す
3. MCP サーバーが `POST {KOBITO_URL}/api/internal/ask` を HTTP で呼び出す
4. kobito サーバーが CLIBridge 経由で Agent B のセッションを立てて回答を取得
5. MCP サーバーがレスポンスを Agent A に返す

### MCP サーバーコード概要

```python
# src/mcp_server/ask_agent.py
from mcp.server.fastmcp import FastMCP
import httpx, os, json

KOBITO_URL = os.environ.get("KOBITO_URL", "http://localhost:3956")

mcp = FastMCP("kobito-ask-agent")

@mcp.tool()
async def ask_agent(agent_id: str, message: str, session_id: str | None = None) -> str:
    """他のエージェントにメッセージを送り、回答を得る"""
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{KOBITO_URL}/api/internal/ask", json=payload)
        if not resp.is_success:
            detail = resp.json().get("detail", resp.text)
            raise ValueError(f"HTTP {resp.status_code}: {detail}")
        return json.dumps(resp.json(), ensure_ascii=False)

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

### KOBITO_URL の設定

`data/mcp_config.json` の `env` フィールドで各エージェントセッションに渡す:

```json
{
  "mcpServers": {
    "kobito": {
      "command": "python",
      "args": ["D:\\AI\\code\\kobito_agents\\src\\mcp_server\\ask_agent.py"],
      "cwd": "D:\\AI\\code\\kobito_agents",
      "env": { "KOBITO_URL": "http://{server_ip}:{port}" }
    }
  }
}
```

`data/mcp_config.json` は CLIBridge 起動時に自動生成される（`_ensure_mcp_config()`）。

---

## 4. バックエンド API

### `POST /api/internal/ask`

エージェントにメッセージを送り、完全な回答を同期的に返す。
内部的には既存の CLIBridge を使用する。

#### リクエスト

```json
{
  "agent_id": "agent_20260404_141316_3af",
  "message": "全リポジトリの一覧を教えて",
  "session_id": null,
  "call_chain": ["system"]
}
```

#### レスポンス

```json
{
  "agent_id": "agent_20260404_141316_3af",
  "agent_name": "コード管理君",
  "session_id": "abc-123-def",
  "response": "現在管理しているリポジトリは..."
}
```

#### 処理フロー

1. `call_chain` でループ検出（循環・最大深度 5）
2. `agent_id` から `ConfigManager` でエージェント情報を取得
3. `call_chain[0]` から発信者名を解決し、メッセージに `[{発信者名}からのメッセージ]` プレフィックスを付与
4. 新規セッション（`session_id` が null）の場合、`agent_communication.md` をシステムプロンプトとして注入
5. `CLIBridge.run_stream()` でメッセージ送信
6. SSE ストリームを内部消費し、全テキストを蓄積
7. `result` イベントから `session_id` を取得
8. 新規セッションの場合、`initiated_by` をセッションメタに記録
9. 蓄積テキスト + session_id をレスポンスとして返却

#### エラー

| ステータス | 条件 |
|-----------|------|
| 404 | `agent_id` が存在しない |
| 400 | ループ検出（循環 or 最大深度超過） |
| 504 | タイムアウト（5分） |

### ルーティング

```
src/server/routes/internal.py
```

`/api/internal/` プレフィックスで、UI からは呼ばれない内部 API であることを明示する。

---

## 5. CLIBridge への MCP 設定注入

### 概要

CLIBridge がセッションを起動する際、`--mcp-config` オプションで
kobito MCP サーバーの設定ファイルを渡す。これにより全エージェントが
`ask_agent` ツールを使用可能になる。

### CLIBridge の変更点

`_build_command()` に以下を追加:

- `--mcp-config data/mcp_config.json` — MCP サーバー設定
- `--append-system-prompt-file shared_instructions.md` — 共通指示（新規セッション時のみ）
- `--append-system-prompt-file agent_communication.md` — エージェント間通信指示（`extra_system_prompt_file` が指定された場合、新規セッション時のみ）

```python
def _build_command(self, model, session_id=None, extra_system_prompt_file=None):
    cmd = [...]
    cmd.extend(["--mcp-config", str(MCP_CONFIG_PATH)])
    if not session_id:
        if _SHARED_INSTRUCTIONS_FILE.exists():
            cmd.extend(["--append-system-prompt-file", str(_SHARED_INSTRUCTIONS_FILE)])
        if extra_system_prompt_file and extra_system_prompt_file.exists():
            cmd.extend(["--append-system-prompt-file", str(extra_system_prompt_file)])
    return cmd
```

---

## 6. エージェント間通信プロンプト

### ファイル

```
assets/prompts/agent_communication.md
```

### 目的

エージェント間通信セッションの開始時にのみ注入される専用システムプロンプト。
受信側エージェントが「ユーザーからではなく、別のエージェントから話しかけられている」
という文脈を正しく認識するための指示を提供する。

### 内容概要

- これは他のエージェントからのメッセージである旨の明示
- 送信者の意図を推測すること
- 担当外の判断はユーザー確認を促すこと
- 丁寧な敬語は不要
- 自分の立場からの判断・意見を添えること

### 適用タイミング

- **新規セッション時のみ** 注入（`session_id` が null の場合）
- 既存セッションの再開時には注入しない（二重注入を防ぐ）

---

## 7. 発信者識別（メッセージプレフィックス）

`/api/internal/ask` は受信側エージェントへのメッセージ先頭に発信者名を付与する。

```
[レプリカからのメッセージ]
今何してる？
```

- `call_chain[0]` のエージェント ID から名前を解決
- ID が存在しない場合は ID をそのまま使用
- `call_chain` が空の場合は `system` として扱う

---

## 8. セッション管理

### Agent B 側のセッション

- `ask_agent` で `session_id` が省略された場合、CLIBridge が新規セッションを作成
- `session_id` が指定された場合、既存セッションを `--resume` で再開
- Agent B のセッションは通常のセッション一覧に表示される
- 新規セッションのメタに呼び出し元情報を記録:

```json
{
  "initiated_by": "system"
}
```

セッション一覧には `via system` のような形で発信元が表示される。

### Agent A 側

- `ask_agent` はツール呼び出しとして A のセッション履歴に自然に残る
- A は返却された `session_id` を保持し、続きの会話に使用可能

---

## 9. ループ防止

### 問題

Agent A が Agent B に質問 → Agent B が Agent A に質問 → 無限ループ

### 対策

`/api/internal/ask` に `call_chain` パラメータを導入:

```json
{
  "agent_id": "agent_B",
  "message": "...",
  "call_chain": ["system"]
}
```

- `call_chain` は呼び出し元エージェントIDの配列（MCP サーバーが自動付与）
- `agent_id` が `call_chain` に含まれていたら 400 エラー（循環ループ）
- `call_chain` の最大長を 5 に制限（A→B→C→D→E まで）

---

## 10. UI への反映

### 注入プロンプトプレビュー

エージェント画面の「注入されるプロンプト」パネルに `agent_communication.md` の節を追加。

- **表示タイミング**: エージェント選択時・セッション選択時・新規会話ボタン押下時
- **取得元**: `GET /api/agents/{agent_id}/system-prompt` のレスポンスに `agent_communication` フィールドを追加
- **ラベル**: `エージェント間通信 (agent_communication.md) ※新規セッション時のみ`

```json
// GET /api/agents/{agent_id}/system-prompt のレスポンス
{
  "content": "# CLAUDE.md の内容...",
  "shared_instructions": "# shared_instructions.md の内容...",
  "agent_communication": "# agent_communication.md の内容..."
}
```

### セッション一覧

`initiated_by` メタがあるセッションには発信元を `via {agent_id}` で表示する。

---

## 11. デフォルトモデル設定

エージェントのデフォルト `model_tier` は `quick`（Sonnet）とする。

- `AgentCreateRequest.model_tier` のデフォルト値: `"quick"`
- 既存エージェントも `data/agents.json` で `model_tier: "quick"` に統一

---

## 12. 実装ファイル一覧

| ファイル | 変更種別 | 内容 |
|---------|---------|------|
| `src/mcp_server/ask_agent.py` | 新規 | MCP サーバー本体 |
| `src/server/routes/internal.py` | 新規 | `POST /api/internal/ask` |
| `src/server/cli_bridge.py` | 修正 | MCP config 注入・extra system prompt 対応 |
| `src/server/routes/agents.py` | 修正 | system-prompt API に `agent_communication` フィールド追加 |
| `src/server/static/app.js` | 修正 | プロンプトプレビューに agent_communication 節追加・表示タイミング修正 |
| `assets/prompts/agent_communication.md` | 新規 | エージェント間通信専用プロンプト |
| `data/mcp_config.json` | 自動生成 | CLIBridge が起動時に生成 |

---

## 13. 依存関係

### 新規パッケージ

| パッケージ | 用途 |
|-----------|------|
| `mcp` | MCP サーバーフレームワーク（`FastMCP` 使用） |
| `httpx` | MCP サーバーから kobito API への非同期 HTTP クライアント |

### 既存活用

- `CLIBridge` — そのまま使用（`extra_system_prompt_file` パラメータ追加）
- `ConfigManager` — エージェント情報取得
- `SessionReader` — セッション一覧表示
