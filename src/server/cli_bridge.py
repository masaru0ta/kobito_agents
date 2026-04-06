"""CLIBridge — 常駐プロセス方式でCLIツールと対話する"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator

import psutil

from server.pid_manager import (
    cleanup_orphaned_processes as _cleanup_orphaned,
    is_process_alive,
    iter_pid_files,
    pid_dir as _pid_dir,
    remove_pid_file as _remove_pid_file,
    terminate_process,
    write_pid_file as _write_pid_file,
)

logger = logging.getLogger(__name__)

# 共通指示ファイル（全エージェントの新規セッションに自動注入）
_SHARED_INSTRUCTIONS_FILE = Path(__file__).resolve().parents[2] / "assets" / "prompts" / "shared_instructions.md"

# MCP設定ファイル
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MCP_CONFIG_DIR = _PROJECT_ROOT / "data" / "mcp_configs"


def _get_kobito_url() -> str:
    """KOBITO_URL を環境変数 → 旧設定ファイルの順で解決する"""
    import os
    url = os.environ.get("KOBITO_URL", "")
    if url:
        return url
    legacy = _PROJECT_ROOT / "data" / "mcp_config.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            url = data["mcpServers"]["kobito"]["env"]["KOBITO_URL"]
            if url:
                return url
        except (KeyError, ValueError):
            pass
    return "http://localhost:3956"


def _ensure_mcp_config(agent_id: str) -> Path:
    """エージェントIDごとの MCP 設定ファイルを生成/更新する"""
    _MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _MCP_CONFIG_DIR / f"mcp_{agent_id}.json"
    config = {
        "mcpServers": {
            "kobito": {
                "command": "python",
                "args": [str(_PROJECT_ROOT / "src" / "mcp_server" / "ask_agent.py")],
                "cwd": str(_PROJECT_ROOT),
                "env": {
                    "KOBITO_URL": _get_kobito_url(),
                    "KOBITO_AGENT_ID": agent_id,
                },
            }
        }
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

# ============================================================
# CLIアダプター（CLI実装が変わる部分を集約）
# ============================================================
# 新しいCLIを追加する場合: CLIAdapter を継承したクラスを作り _ADAPTERS に登録するだけでよい

class CLIAdapter:
    """各CLIツールの実装差分を集約する基底クラス"""
    _models: dict[str, str] = {}

    def resolve_model(self, tier: str) -> str:
        model = self._models.get(tier)
        if model is None:
            raise ValueError(f"不明なモデルティア: {tier}")
        return model

    async def run_stream(
        self,
        bridge: "CLIBridge",
        project_path: str,
        prompt: str,
        model: str,
        session_id: str | None,
        extra_system_prompt_file: Path | None,
        agent_id: str,
    ) -> AsyncGenerator[dict, None]:
        raise NotImplementedError
        yield  # AsyncGenerator として認識させるためのマーカー


class ClaudeAdapter(CLIAdapter):
    _models = {"quick": "sonnet", "deep": "opus"}

    @staticmethod
    def find_binary() -> str:
        path = shutil.which("claude")
        if path is None:
            raise FileNotFoundError("claudeコマンドが見つかりません")
        return path

    def build_command(
        self,
        model: str,
        session_id: str | None = None,
        extra_system_prompt_file: Path | None = None,
        agent_id: str = "",
    ) -> list[str]:
        """Claude Code CLI のコマンドを構築する"""
        mcp_config = _ensure_mcp_config(agent_id)
        cmd = [
            self.find_binary(),
            "-p", "",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "--mcp-config", str(mcp_config),
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            if _SHARED_INSTRUCTIONS_FILE.exists():
                cmd.extend(["--append-system-prompt-file", str(_SHARED_INSTRUCTIONS_FILE)])
            if extra_system_prompt_file and extra_system_prompt_file.exists():
                cmd.extend(["--append-system-prompt-file", str(extra_system_prompt_file)])
        return cmd

    async def run_stream(self, bridge, project_path, prompt, model, session_id, extra_system_prompt_file, agent_id):
        async for event in bridge._run_claude_stream(
            project_path, prompt, model, session_id, extra_system_prompt_file, agent_id
        ):
            yield event


class CodexAdapter(CLIAdapter):
    # ChatGPT アカウントでは gpt-5 のみ対応（OpenAI API キーなら o4-mini/o3 に変更可）
    _models = {"quick": "gpt-5", "deep": "gpt-5"}

    async def run_stream(self, bridge, project_path, prompt, model, session_id, extra_system_prompt_file, agent_id):
        """Codex CLI を実行する。session_id があればスレッドを再開する。

        出力フォーマット（codex exec --json）:
          {"type":"thread.started","thread_id":"<uuid7>"}
          {"type":"turn.started"}
          {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
          {"type":"turn.completed","usage":{...}}
        """
        codex = shutil.which("codex")
        if codex is None:
            raise FileNotFoundError("codexコマンドが見つかりません")

        # codex exec [--full-auto] [-m model] [--json] [resume <session_id>] <prompt>
        cmd = [codex, "exec", "--dangerously-bypass-approvals-and-sandbox", "-m", model, "--json"]

        # 新規セッションのみ共通指示 + extra_system_prompt_file を -c instructions=... で注入
        if not session_id:
            parts = []
            if _SHARED_INSTRUCTIONS_FILE.exists():
                parts.append(_SHARED_INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip())
            if extra_system_prompt_file and extra_system_prompt_file.exists():
                parts.append(extra_system_prompt_file.read_text(encoding="utf-8").strip())
            if parts:
                cmd.extend(["-c", f"developer_instructions={json.dumps(chr(10).join(parts), ensure_ascii=False)}"])

        if session_id:
            cmd.extend(["resume", session_id])
        cmd.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path,
        )
        assert proc.stdout is not None

        thread_id = ""
        got_turn_completed = False
        error_msg = ""
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[CODEX] non-JSON stdout: {line[:200]}", flush=True)
                continue

            etype = event.get("type", "")
            if etype == "thread.started":
                thread_id = event.get("thread_id", "")
            elif etype == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    yield {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": item["text"]}]},
                    }
            elif etype == "turn.completed":
                got_turn_completed = True
                yield {"type": "result", "session_id": thread_id, "result": ""}
            elif etype in ("error", "turn.failed"):
                raw_msg = event.get("message") or event.get("error", {}).get("message", "")
                # メッセージが JSON 文字列の場合はパースして取り出す
                try:
                    inner = json.loads(raw_msg)
                    error_msg = inner.get("error", {}).get("message", raw_msg)
                except (json.JSONDecodeError, TypeError):
                    error_msg = raw_msg

        await proc.wait()

        if not got_turn_completed:
            if not error_msg:
                stderr_bytes = await proc.stderr.read()
                error_msg = stderr_bytes.decode("utf-8", errors="replace").strip() or "(出力なし)"
            print(f"[CODEX] 異常終了 returncode={proc.returncode} error={error_msg[:300]}", flush=True)
            raise RuntimeError(f"codex エラー: {error_msg[:300]}")


_ADAPTERS: dict[str, CLIAdapter] = {
    "claude": ClaudeAdapter(),
    "codex":  CodexAdapter(),
}


def resolve_model(cli: str, model_tier: str) -> str:
    """CLIツール種別とモデルティアからモデル名を返す"""
    adapter = _ADAPTERS.get(cli)
    if adapter is None:
        raise ValueError(f"不明なCLIツール: {cli}")
    return adapter.resolve_model(model_tier)


@dataclass
class StreamEvent:
    """stream-json 1行から抽出した情報"""
    event_type: str
    text: str
    tool_uses: list[dict]
    session_id: str
    result_text: str


def parse_stream_event(event: dict) -> StreamEvent:
    """stream-json 1行をパースする"""
    etype = event.get("type", "")
    text = ""
    tool_uses = []
    session_id = ""
    result_text = ""

    if etype == "assistant":
        for item in event.get("message", {}).get("content", []):
            if item.get("type") == "text":
                text = item["text"]
            elif item.get("type") == "tool_use":
                tool_uses.append(item)
    elif etype == "result":
        session_id = event.get("session_id", "")
        result_text = event.get("result", "")

    return StreamEvent(
        event_type=etype,
        text=text,
        tool_uses=tool_uses,
        session_id=session_id,
        result_text=result_text,
    )


# ============================================================
# JSONL 末尾確認
# ============================================================

def _jsonl_info(project_path: str, session_id: str) -> tuple[str | None, float]:
    """JSONLの末尾を読み、(最後のuser/assistantのrole, ファイルmtime) を返す。"""
    if not session_id or session_id.startswith("new-"):
        return None, 0.0
    project_hash = (
        project_path.replace("\\", "-").replace(":", "-")
        .replace("/", "-").replace("_", "-")
    )
    jsonl_path = Path.home() / ".claude" / "projects" / project_hash / f"{session_id}.jsonl"
    if not jsonl_path.exists():
        return None, 0.0
    try:
        mtime = jsonl_path.stat().st_mtime
        # 末尾16KBだけ読む（大きなファイルへの配慮）
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 16384))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                t = data.get("type")
                if t in ("user", "assistant"):
                    return t, mtime
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return None, 0.0


# ============================================================
# 推論中判定（TCP コネクション検査）
# ============================================================

def _has_api_connection(pid: int) -> bool:
    """プロセス（と子プロセス）が port 443 への TCP 接続を持っているか判定する。
    接続があれば Anthropic API と通信中 = 推論中と見なす。"""
    try:
        proc = psutil.Process(pid)
        for p in [proc] + proc.children(recursive=True):
            try:
                for conn in p.connections():
                    if (conn.raddr
                            and conn.raddr.port == 443
                            and conn.status == "ESTABLISHED"):
                        return True
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def cleanup_orphaned_processes(project_path: str) -> None:
    """サーバー起動時に呼ぶ。孤児プロセスのクリーンアップ。"""
    _cleanup_orphaned(project_path, _has_api_connection)


def _judge_inferring(
    awaiting_response: bool,
    has_connection: bool,
    jsonl_updated: bool,
    last_role: str | None,
    jsonl_stable: bool,
) -> bool:
    """推論中かどうかを判定する共通ロジック。

    ロジック表:
    | 送信済 | TCP接続 | JSONL更新(送信後) | JSONL末尾  | JSONL安定(3秒) | 判定   |
    |--------|---------|-----------------|-----------|---------------|--------|
    | No     | -       | -               | -         | -             | 待機   |
    | Yes    | 切れ    | -               | -         | -             | 完了   |
    | Yes    | 接続中  | No              | -         | -             | 推論中 |
    | Yes    | 接続中  | Yes             | user      | -             | 推論中 |
    | Yes    | 接続中  | Yes             | assistant | No            | 推論中 |
    | Yes    | 接続中  | Yes             | assistant | Yes           | 完了   |
    """
    if not awaiting_response:
        return False
    if not has_connection:
        return False
    if not jsonl_updated:
        return True
    if last_role != "assistant":
        return True
    if not jsonl_stable:
        return True
    return False


# ============================================================
# ManagedProcess
# ============================================================

JSONL_STABLE_SECS = 3.0  # JSONL mtime がこの秒数以上変化なし → 安定


@dataclass
class ManagedProcess:
    """セッションに紐づく常駐claudeプロセス"""
    proc: subprocess.Popen
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_thread: threading.Thread | None = None
    session_id: str = ""
    model: str = ""
    project_path: str = ""
    last_used: float = field(default_factory=time.time)
    message_sent_at: float = 0.0    # 最後にメッセージを送信した時刻（0=未送信）
    last_seen_jsonl_mtime: float = 0.0  # 最後に観測したJSONL mtime
    last_mtime_change_at: float = 0.0   # JSONL mtime が最後に変化した時刻
    _loop: asyncio.AbstractEventLoop | None = None

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def start_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        """stdoutを非同期キューに流すスレッドを開始"""
        self._loop = loop

        def _read():
            try:
                for raw_line in self.proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        etype = data.get("type", "?")
                        logger.debug("READER event=%s sid=%s", etype, self.session_id[:8])
                        loop.call_soon_threadsafe(self.queue.put_nowait, data)
                    except json.JSONDecodeError:
                        logger.warning("READER JSON parse error: %s", line[:80])
            except Exception as e:
                logger.error("READER exception: %s", e)
            # stdout が切れた = プロセスとの通信不能 → killしてプールから除去
            logger.info("READER stdout切断 sid=%s pid=%s", self.session_id[:8], self.proc.pid)
            try:
                self.proc.terminate()
            except Exception:
                pass
            _remove_pid_file(self.project_path, self.session_id)
            loop.call_soon_threadsafe(self.queue.put_nowait, {"type": "_process_exit"})

        self.reader_thread = threading.Thread(target=_read, daemon=True)
        self.reader_thread.start()

    def send_message(self, content: str) -> None:
        """stdinにNDJSONメッセージを書き込む"""
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": content},
        }, ensure_ascii=False)
        self.proc.stdin.write((msg + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        now = time.time()
        self.last_used = now
        self.message_sent_at = now
        # JSONL安定性トラッキングをリセット（前のメッセージの状態を引き継がないため）
        self.last_seen_jsonl_mtime = 0.0
        self.last_mtime_change_at = 0.0
        logger.debug("message_sent sid=%s pid=%s", self.session_id, self.proc.pid)

    def kill(self) -> None:
        """プロセスを終了する"""
        try:
            if self.alive:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ============================================================
# CLIBridge
# ============================================================

class CLIBridge:
    """常駐プロセスプールを管理するCLIブリッジ"""

    IDLE_TIMEOUT = 600  # 10分

    def __init__(self):
        self._pool: dict[str, ManagedProcess] = {}
        self._pool_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        atexit.register(self._kill_all_sync)

    def _spawn_process(self, project_path: str, cmd: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_path,
        )

    def _pool_key(self, project_path: str, session_id: str | None) -> str:
        """プールのキーを生成"""
        return f"{project_path}::{session_id or 'new-' + str(id(asyncio.current_task()))}"

    async def _get_or_create_process(
        self,
        project_path: str,
        model: str,
        session_id: str | None,
        extra_system_prompt_file: Path | None = None,
        agent_id: str = "",
    ) -> tuple[ManagedProcess, str]:
        """既存プロセスを取得、またはなければ新規作成"""
        key = self._pool_key(project_path, session_id)

        async with self._pool_lock:
            mp = self._pool.get(key)

            # 既存プロセスがあるがモデルが違う場合は終了して再作成
            if mp and mp.alive and mp.model != model:
                logger.info(f"モデル変更検出 ({mp.model} → {model}): プロセス再起動")
                mp.kill()
                mp = None

            # 既存プロセスが死んでいる場合は除去
            if mp and not mp.alive:
                logger.info(f"プロセス死亡検出: {key}")
                del self._pool[key]
                mp = None

            if mp:
                mp.last_used = time.time()
                return mp, key

            # 新規作成
            claude = _ADAPTERS["claude"]
            assert isinstance(claude, ClaudeAdapter)
            cmd = claude.build_command(model, session_id, extra_system_prompt_file, agent_id)
            proc = self._spawn_process(project_path, cmd)
            mp = ManagedProcess(proc=proc, model=model, session_id=session_id or "", project_path=project_path)
            loop = asyncio.get_running_loop()
            mp.start_reader(loop)
            if session_id:
                _write_pid_file(project_path, session_id, proc.pid)
            self._pool[key] = mp
            logger.info(f"プロセス起動: {key} (model={model})")

            # クリーンアップタスクが未起動なら開始
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            return mp, key

    async def run_stream(
        self,
        project_path: str,
        prompt: str,
        model: str,
        session_id: str | None = None,
        extra_system_prompt_file: Path | None = None,
        agent_id: str = "",
        cli: str = "claude",
    ) -> AsyncGenerator[dict, None]:
        """メッセージを送信し、resultイベントまでのストリームをyieldする"""
        adapter = _ADAPTERS.get(cli)
        if adapter is None:
            raise ValueError(f"不明なCLIツール: {cli}")
        async for event in adapter.run_stream(
            self, project_path, prompt, model, session_id, extra_system_prompt_file, agent_id
        ):
            yield event

    async def _run_claude_stream(
        self,
        project_path: str,
        prompt: str,
        model: str,
        session_id: str | None = None,
        extra_system_prompt_file: Path | None = None,
        agent_id: str = "",
    ) -> AsyncGenerator[dict, None]:
        """Claude Code 常駐プロセスへメッセージを送信し、resultイベントまでをyieldする"""
        mp, key = await self._get_or_create_process(
            project_path, model, session_id, extra_system_prompt_file, agent_id,
        )

        async with mp.lock:
            # キューに溜まっている古いイベントを排出
            while not mp.queue.empty():
                try:
                    mp.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            mp.send_message(prompt)

            last_event_type = None
            exit_reason = "unknown"
            try:
                while True:
                    try:
                        # 15秒待ってイベントが来なければ _ping を yield して接続を維持する
                        # （event_stream側でwait_forを使うとジェネレータが破壊されるためここで管理）
                        event = await asyncio.wait_for(mp.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield {"type": "_ping"}
                        continue

                    etype = event.get("type")
                    last_event_type = etype

                    if etype == "_process_exit":
                        mp.message_sent_at = 0.0
                        exit_reason = "process_exit"
                        async with self._pool_lock:
                            self._pool.pop(key, None)
                        break

                    # resultイベントでsession_idを更新（ストリーム終端として使用）
                    if etype == "result":
                        new_sid = event.get("session_id", "")
                        if new_sid and not mp.session_id:
                            mp.session_id = new_sid
                            # PID ファイルを確定セッションIDで書き直す
                            _write_pid_file(project_path, new_sid, mp.proc.pid)
                            real_key = self._pool_key(project_path, new_sid)
                            async with self._pool_lock:
                                if key in self._pool:
                                    self._pool[real_key] = self._pool.pop(key)
                                    key = real_key

                    yield event

                    # resultはHTTPストリーム終端として使う（推論完了の判断はしない）
                    if etype == "result":
                        exit_reason = "result"
                        logger.debug("result event sid=%s pid=%s", mp.session_id, mp.proc.pid)
                        break
            finally:
                logger.info("run_stream終了 reason=%s last_event=%s sid=%s pid=%s", exit_reason, last_event_type, mp.session_id[:8] if mp.session_id else "?", mp.proc.pid)

    async def _cleanup_loop(self) -> None:
        """アイドルプロセスを定期的に終了する"""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            to_remove = []
            async with self._pool_lock:
                for key, mp in self._pool.items():
                    if not mp.alive:
                        to_remove.append(key)
                    elif now - mp.last_used > self.IDLE_TIMEOUT:
                        logger.info(f"アイドルタイムアウト: {key}")
                        mp.kill()
                        to_remove.append(key)
                for key in to_remove:
                    self._pool.pop(key, None)
            if not self._pool:
                break

    def _kill_all_sync(self) -> None:
        """全プロセスを即座に終了する（同期版）"""
        for key, mp in list(self._pool.items()):
            _remove_pid_file(mp.project_path, mp.session_id)
            mp.kill()
        self._pool.clear()

    async def shutdown(self) -> None:
        """サーバー停止時: 推論中プロセスの完了を待ってから全終了する"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        # 推論中のプロセスがあれば完了を待つ（最大60秒）
        for _ in range(60):
            any_inferring = any(
                _has_api_connection(mp.proc.pid)
                for mp in self._pool.values() if mp.alive
            )
            if not any_inferring:
                break
            await asyncio.sleep(1)
        self._kill_all_sync()

    def inferring_session_ids(self, project_path: str) -> list[str]:
        """推論中のセッションIDを返す。
        プロセスプール内のプロセスと、PIDファイルで追跡中の孤児プロセスの両方を検査する。
        判定ロジックは _judge_inferring() を参照。"""
        prefix = f"{project_path}::"
        result = []
        pool_sids = set()
        now = time.time()

        # 1. プロセスプール内のプロセスを検査
        for key, mp in self._pool.items():
            if not key.startswith(prefix) or not mp.alive:
                continue
            sid = key[len(prefix):]
            if sid.startswith("new-"):
                continue
            pool_sids.add(sid)

            last_role, jsonl_mtime = _jsonl_info(project_path, sid)

            # JSONL安定性トラッキング更新
            if jsonl_mtime > 0 and jsonl_mtime != mp.last_seen_jsonl_mtime:
                mp.last_seen_jsonl_mtime = jsonl_mtime
                mp.last_mtime_change_at = now

            has_conn = _has_api_connection(mp.proc.pid)
            jsonl_updated = jsonl_mtime > mp.message_sent_at
            jsonl_stable = (mp.last_mtime_change_at > 0
                            and (now - mp.last_mtime_change_at) >= JSONL_STABLE_SECS)

            inferring = _judge_inferring(
                awaiting_response=(mp.message_sent_at > 0.0),
                has_connection=has_conn,
                jsonl_updated=jsonl_updated,
                last_role=last_role,
                jsonl_stable=jsonl_stable,
            )
            if inferring:
                result.append(sid)
            elif mp.message_sent_at > 0.0:
                # 推論完了 → 送信状態をリセット
                mp.message_sent_at = 0.0

        # 2. PID ファイルから孤児プロセスを検査
        for pid_file, sid, pid in iter_pid_files(project_path):
            if sid in pool_sids:
                continue  # プール内で既に検査済み
            if not is_process_alive(pid):
                try:
                    pid_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            has_conn = _has_api_connection(pid)
            last_role, _ = _jsonl_info(project_path, sid)
            if has_conn and last_role != "assistant":
                result.append(sid)
            else:
                # TCP切れ or JSONL=assistant → 推論終了済みの孤児 → kill してクリーンアップ
                terminate_process(pid)
                try:
                    pid_file.unlink(missing_ok=True)
                except Exception:
                    pass
                logger.info(f"孤児プロセス終了: PID={pid} session={sid}")

        return result

    def process_debug_info(self, project_path: str) -> list[dict]:
        """デバッグ用: プロセスプールとPIDファイルの詳細情報を返す"""
        prefix = f"{project_path}::"
        result = []
        pool_sids = set()
        now = time.time()

        # 1. プロセスプール内
        for key, mp in self._pool.items():
            if key.startswith(prefix):
                sid = key[len(prefix):]
                pool_sids.add(sid)
                pid = mp.proc.pid
                alive = mp.alive
                has_conn = _has_api_connection(pid) if alive else False
                last_role, jsonl_mtime = _jsonl_info(project_path, sid) if not sid.startswith("new-") else (None, 0.0)
                awaiting_response = alive and mp.message_sent_at > 0
                jsonl_updated = jsonl_mtime > mp.message_sent_at if awaiting_response else False
                jsonl_stable = (mp.last_mtime_change_at > 0
                                and (now - mp.last_mtime_change_at) >= JSONL_STABLE_SECS)
                jsonl_stable_secs = round(now - mp.last_mtime_change_at, 1) if mp.last_mtime_change_at > 0 else None
                inferring = _judge_inferring(
                    awaiting_response=awaiting_response,
                    has_connection=has_conn,
                    jsonl_updated=jsonl_updated,
                    last_role=last_role,
                    jsonl_stable=jsonl_stable,
                )
                result.append({
                    "session_id": sid,
                    "pid": pid,
                    "source": "pool",
                    "alive": alive,
                    "connected": has_conn,
                    "awaiting_response": awaiting_response,
                    "jsonl_last_role": last_role,
                    "jsonl_updated": jsonl_updated,
                    "jsonl_stable": jsonl_stable,
                    "jsonl_stable_secs": jsonl_stable_secs,
                    "inferring": inferring,
                })

        # 2. PIDファイル（孤児）
        for pid_file, sid, pid in iter_pid_files(project_path):
            if sid in pool_sids:
                continue
            alive = is_process_alive(pid)
            has_conn = _has_api_connection(pid) if alive else False
            last_role, _ = _jsonl_info(project_path, sid)
            inferring = has_conn and last_role != "assistant"
            result.append({
                "session_id": sid,
                "pid": pid,
                "source": "pidfile",
                "alive": alive,
                "connected": has_conn,
                "awaiting_response": None,
                "jsonl_last_role": last_role,
                "jsonl_updated": None,
                "jsonl_stable": None,
                "jsonl_stable_secs": None,
                "inferring": inferring,
            })

        return result

    async def stop_session(self, project_path: str, session_id: str) -> bool:
        """指定セッションのプロセスを強制終了する。終了できたら True を返す。"""
        prefix = f"{project_path}::"
        key = f"{prefix}{session_id}"
        async with self._pool_lock:
            mp = self._pool.pop(key, None)
        if mp:
            mp.message_sent_at = 0.0
            _remove_pid_file(project_path, session_id)
            mp.kill()
            print(f"[STOP] プロセス停止 sid={session_id} pid={mp.proc.pid}", flush=True)
            return True
        # プールにない場合はPIDファイルを確認
        pid_file = Path(project_path) / ".kobito" / "alive" / f"{session_id}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                psutil.Process(pid).terminate()
                pid_file.unlink(missing_ok=True)
                print(f"[STOP] 孤児プロセス停止 sid={session_id} pid={pid}", flush=True)
                return True
            except Exception:
                pass
        return False

    def launch_cli(self, project_path: str, session_id: str | None = None) -> None:
        """ターミナルでCLIを起動する（Windowsのみ・Claude専用）"""
        claude = _ADAPTERS["claude"]
        assert isinstance(claude, ClaudeAdapter)
        cmd_parts = [claude.find_binary()]
        if session_id:
            cmd_parts.extend(["--resume", session_id])
        cmd_str = " ".join(cmd_parts)
        subprocess.Popen(
            f'start cmd /k "cd /d {project_path} && {cmd_str}"',
            shell=True,
        )
