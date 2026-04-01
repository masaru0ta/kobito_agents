"""CLIBridge — CLIツールを呼び出してプロンプトを送り、ストリーミングで応答を返す"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator


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
    """claude -p のstream-json 1行をパースする"""
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


class CLIBridge:
    @staticmethod
    def _find_claude() -> str:
        path = shutil.which("claude")
        if path is None:
            raise FileNotFoundError("claudeコマンドが見つかりません")
        return path

    def build_command(
        self,
        project_path: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
    ) -> tuple[list[str], str]:
        """コマンドとcwdを返す"""
        cmd = [
            self._find_claude(), "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        else:
            if system_prompt:
                cmd.extend(["--system-prompt", system_prompt])
        return cmd, project_path

    async def run_stream(
        self,
        project_path: str,
        prompt: str,
        model: str,
        session_id: str | None = None,
        system_prompt: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """claude -p をストリーミング実行。stdoutの各JSON行をyieldする"""
        cmd, cwd = self.build_command(project_path, model, session_id, system_prompt)

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )

        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _read_stdout():
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    loop.call_soon_threadsafe(queue.put_nowait, data)
                except json.JSONDecodeError:
                    pass
            proc.wait()
            loop.call_soon_threadsafe(queue.put_nowait, None)

        reader = threading.Thread(target=_read_stdout, daemon=True)
        reader.start()

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

        reader.join()

        if proc.returncode != 0:
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"claude -p 失敗 (rc={proc.returncode}): {stderr_text}")

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
