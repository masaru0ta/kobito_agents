"""ChatManager — セッション一覧・履歴取得、メッセージ送信、要約生成を統合"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncGenerator

from server.cli_bridge import CLIBridge, parse_stream_event, resolve_model
from server.config import ConfigManager
from server.session_reader import SessionMessage, SessionReader, SessionSummary


class ChatManager:
    def __init__(
        self,
        config_manager: ConfigManager,
        session_reader: SessionReader,
        cli_bridge: CLIBridge,
    ):
        self._config = config_manager
        self._reader = session_reader
        self._bridge = cli_bridge

    def list_sessions(self, agent_id: str) -> list[SessionSummary]:
        agent = self._config.get_agent(agent_id)
        return self._reader.list_sessions(agent.path)

    def get_session_history(self, agent_id: str, session_id: str) -> list[SessionMessage]:
        agent = self._config.get_agent(agent_id)
        return self._reader.read_session(agent.path, session_id)

    def hide_session(self, agent_id: str, session_id: str) -> None:
        """セッションを非表示にする"""
        agent = self._config.get_agent(agent_id)
        meta_dir = Path(agent.path) / ".kobito" / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path = meta_dir / f"{session_id}.json"

        # 既存メタデータがあればマージ
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["hidden"] = True
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    async def summarize_session(self, agent_id: str, session_id: str) -> dict:
        """会話を要約してtitle/summaryを生成・保存する"""
        agent = self._config.get_agent(agent_id)
        messages = self._reader.read_session(agent.path, session_id)

        result = await self._bridge.summarize(agent, messages)

        # メタデータに保存
        meta_dir = Path(agent.path) / ".kobito" / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path = meta_dir / f"{session_id}.json"

        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["title"] = result["title"]
        meta["summary"] = result["summary"]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        return result
