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
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# モデルティアからモデル名へのマッピング
MODEL_MAP = {
    "claude": {
        "deep": "opus",
        "quick": "sonnet",
    },
    "codex": {
        "deep": "o3",
        "quick": "o4-mini",
    },
}


def resolve_model(cli: str, model_tier: str) -> str:
    """CLIツール種別とモデルティアからモデル名を返す"""
    cli_map = MODEL_MAP.get(cli)
    if cli_map is None:
        raise ValueError(f"不明なCLIツール: {cli}")
    model = cli_map.get(model_tier)
    if model is None:
        raise ValueError(f"不明なモデルティア: {model_tier}")
    return model


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


@dataclass
class ManagedProcess:
    """セッションに紐づく常駐claudeプロセス"""
    proc: subprocess.Popen
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_thread: threading.Thread | None = None
    session_id: str = ""
    model: str = ""
    last_used: float = field(default_factory=time.time)
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
                        loop.call_soon_threadsafe(self.queue.put_nowait, data)
                    except json.JSONDecodeError:
                        pass
            except Exception:
                pass
            # プロセス終了を通知
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
        self.last_used = time.time()

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


class CLIBridge:
    """常駐プロセスプールを管理するCLIブリッジ"""

    IDLE_TIMEOUT = 600  # 10分

    def __init__(self):
        self._pool: dict[str, ManagedProcess] = {}
        self._pool_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        atexit.register(self._kill_all_sync)

    @staticmethod
    def _find_claude() -> str:
        path = shutil.which("claude")
        if path is None:
            raise FileNotFoundError("claudeコマンドが見つかりません")
        return path

    def _build_command(
        self,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
    ) -> list[str]:
        cmd = [
            self._find_claude(),
            "-p", "",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])
        return cmd

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
        system_prompt: str | None,
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
            cmd = self._build_command(model, session_id, system_prompt)
            proc = self._spawn_process(project_path, cmd)
            mp = ManagedProcess(proc=proc, model=model, session_id=session_id or "")
            loop = asyncio.get_running_loop()
            mp.start_reader(loop)
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
        system_prompt: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """メッセージを送信し、resultイベントまでのストリームをyieldする"""
        mp, key = await self._get_or_create_process(
            project_path, model, session_id, system_prompt,
        )

        async with mp.lock:
            # キューに溜まっている古いイベントを排出
            while not mp.queue.empty():
                try:
                    mp.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            mp.send_message(prompt)

            while True:
                try:
                    event = await asyncio.wait_for(mp.queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning(f"タイムアウト: {key}")
                    break

                if event.get("type") == "_process_exit":
                    # プロセスが死んだ場合、プールから除去
                    async with self._pool_lock:
                        self._pool.pop(key, None)
                    break

                # resultイベントでsession_idを更新
                if event.get("type") == "result":
                    new_sid = event.get("session_id", "")
                    if new_sid and not mp.session_id:
                        mp.session_id = new_sid
                        # 新規セッションのキーを確定IDに付け替え
                        real_key = self._pool_key(project_path, new_sid)
                        async with self._pool_lock:
                            if key in self._pool:
                                self._pool[real_key] = self._pool.pop(key)
                                key = real_key

                yield event

                if event.get("type") == "result":
                    break

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
        """サーバー停止時に全プロセスを終了する（同期版）"""
        for key, mp in list(self._pool.items()):
            mp.kill()
        self._pool.clear()

    async def shutdown(self) -> None:
        """サーバー停止時に全プロセスを終了する（非同期版）"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._kill_all_sync()

    def responding_session_ids(self, project_path: str) -> list[str]:
        """指定プロジェクトで現在応答処理中（ロック取得中）のセッションIDを返す"""
        prefix = f"{project_path}::"
        result = []
        for key, mp in self._pool.items():
            if key.startswith(prefix) and mp.alive and mp.lock.locked():
                sid = key[len(prefix):]
                if not sid.startswith("new-"):
                    result.append(sid)
        return result

    def active_session_ids(self, project_path: str) -> list[str]:
        """指定プロジェクトで稼働中のセッションIDを返す"""
        prefix = f"{project_path}::"
        result = []
        for key, mp in self._pool.items():
            if key.startswith(prefix) and mp.alive:
                sid = key[len(prefix):]
                # 新規セッション用の一時キー（new-...）は除外
                if not sid.startswith("new-"):
                    result.append(sid)
        return result

    def launch_cli(self, project_path: str, session_id: str | None = None) -> None:
        """ターミナルでCLIを起動する（Windowsのみ）"""
        cmd_parts = ["claude"]
        if session_id:
            cmd_parts.extend(["--resume", session_id])
        cmd_str = " ".join(cmd_parts)
        subprocess.Popen(
            f'start cmd /k "cd /d {project_path} && {cmd_str}"',
            shell=True,
        )
