"""CLIBridge — 常駐プロセス方式でCLIツールと対話する"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator

import psutil

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


# ============================================================
# PID ファイル管理
# ============================================================

def _alive_dir(project_path: str) -> Path:
    """PID ファイル置き場: {project}/.kobito/alive/"""
    d = Path(project_path) / ".kobito" / "alive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_pid_file(project_path: str, session_id: str, pid: int) -> None:
    """プロセス起動時に PID を記録する"""
    if not session_id or session_id.startswith("new-"):
        return
    path = _alive_dir(project_path) / f"{session_id}.pid"
    path.write_text(str(pid), encoding="utf-8")


def _remove_pid_file(project_path: str, session_id: str) -> None:
    """プロセス終了時に PID ファイルを削除する"""
    if not session_id:
        return
    path = _alive_dir(project_path) / f"{session_id}.pid"
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


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
                for conn in p.net_connections():
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
    """サーバー起動時に呼ぶ。PID ファイルから孤児プロセスを検出し、
    推論が終わっているものは kill してクリーンアップする。"""
    d = Path(project_path) / ".kobito" / "alive"
    if not d.exists():
        return
    for pid_file in d.glob("*.pid"):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)  # 生存確認
            if not _has_api_connection(pid):
                # 推論終了済み → kill
                try:
                    psutil.Process(pid).terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                pid_file.unlink(missing_ok=True)
                logger.info(f"孤児プロセス終了: PID={pid} session={pid_file.stem}")
            else:
                logger.info(f"孤児プロセス推論中: PID={pid} session={pid_file.stem}")
        except (OSError, ValueError):
            # プロセスが既に死んでいる or 不正ファイル
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass


# ============================================================
# ManagedProcess
# ============================================================

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

            try:
                while True:
                    try:
                        event = await asyncio.wait_for(mp.queue.get(), timeout=300)
                    except asyncio.TimeoutError:
                        logger.warning(f"タイムアウト: {key}")
                        break

                    if event.get("type") == "_process_exit":
                        async with self._pool_lock:
                            self._pool.pop(key, None)
                        break

                    # resultイベントでsession_idを更新
                    if event.get("type") == "result":
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

                    if event.get("type") == "result":
                        break
            finally:
                pass

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
        プロセスプール内のプロセスと、PIDファイルで追跡中の孤児プロセスの両方を検査する。"""
        prefix = f"{project_path}::"
        result = []
        pool_sids = set()

        # 1. プロセスプール内のプロセスを検査
        for key, mp in self._pool.items():
            if key.startswith(prefix) and mp.alive:
                sid = key[len(prefix):]
                if not sid.startswith("new-"):
                    pool_sids.add(sid)
                    if _has_api_connection(mp.proc.pid):
                        result.append(sid)

        # 2. PID ファイルから孤児プロセスを検査
        d = Path(project_path) / ".kobito" / "alive"
        if d.exists():
            for pid_file in d.glob("*.pid"):
                sid = pid_file.stem
                if sid in pool_sids:
                    continue  # プール内で既に検査済み
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                    os.kill(pid, 0)  # 生存確認
                    if _has_api_connection(pid):
                        result.append(sid)
                    else:
                        # 推論終了済みの孤児 → kill してクリーンアップ
                        try:
                            psutil.Process(pid).terminate()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                        pid_file.unlink(missing_ok=True)
                        logger.info(f"孤児プロセス終了: PID={pid} session={sid}")
                except (OSError, ValueError):
                    # プロセス死亡 or 不正ファイル → PID ファイル削除
                    try:
                        pid_file.unlink(missing_ok=True)
                    except Exception:
                        pass

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
