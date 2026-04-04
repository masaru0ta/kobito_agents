"""ConfigManager — エージェント登録情報の管理"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class AgentInfo(BaseModel):
    id: str
    name: str
    path: str
    description: str = ""
    cli: str = "claude"
    model_tier: str = "deep"
    system_prompt: str = ""


class AgentNotFoundError(Exception):
    pass


class ConfigManager:
    def __init__(self, data_dir: Path | str, system_path: str):
        self._data_dir = Path(data_dir)
        self._system_path = system_path
        self._agents_file = self._data_dir / "agents.json"
        self._settings_file = self._data_dir / "settings.json"
        self._ensure_system_agent()

    def _ensure_system_agent(self) -> None:
        """agents.jsonが存在しなければsystemエージェントを含むファイルを作成する"""
        if self._agents_file.exists():
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        agents = [
            {
                "id": "system",
                "name": "レプリカ",
                "path": self._system_path,
                "description": "システム管理エージェント",
                "cli": "claude",
                "model_tier": "deep",
            }
        ]
        self._write_agents(agents)

    def _read_agents(self) -> list[dict]:
        return json.loads(self._agents_file.read_text(encoding="utf-8"))

    def _write_agents(self, agents: list[dict]) -> None:
        self._agents_file.write_text(
            json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _read_system_prompt(self, path: str) -> str:
        claude_md = Path(path) / "CLAUDE.md"
        if claude_md.exists():
            return claude_md.read_text(encoding="utf-8")
        return ""

    def list_agents(self) -> list[AgentInfo]:
        agents = self._read_agents()
        return [
            AgentInfo(
                **agent,
                system_prompt=self._read_system_prompt(agent["path"]),
            )
            for agent in agents
        ]

    def get_agent(self, agent_id: str) -> AgentInfo:
        for agent in self._read_agents():
            if agent["id"] == agent_id:
                return AgentInfo(
                    **agent,
                    system_prompt=self._read_system_prompt(agent["path"]),
                )
        raise AgentNotFoundError(f"エージェント '{agent_id}' が見つかりません")

    def update_agent(self, agent_id: str, **kwargs) -> AgentInfo:
        """name, description, model_tier等を更新する"""
        agents = self._read_agents()
        for agent in agents:
            if agent["id"] == agent_id:
                for key, value in kwargs.items():
                    if key in ("name", "description", "model_tier", "cli") and value is not None:
                        agent[key] = value
                self._write_agents(agents)
                return AgentInfo(
                    **agent,
                    system_prompt=self._read_system_prompt(agent["path"]),
                )
        raise AgentNotFoundError(f"エージェント '{agent_id}' が見つかりません")

    def get_system_prompt(self, agent_id: str) -> str:
        agent = self.get_agent(agent_id)
        return agent.system_prompt

    def update_system_prompt(self, agent_id: str, content: str) -> None:
        agent = self.get_agent(agent_id)
        (Path(agent.path) / "CLAUDE.md").write_text(content, encoding="utf-8")

    def get_setting(self, key: str, default=None):
        if not self._settings_file.exists():
            return default
        data = json.loads(self._settings_file.read_text(encoding="utf-8"))
        return data.get(key, default)

    def set_setting(self, key: str, value) -> None:
        data = {}
        if self._settings_file.exists():
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
        data[key] = value
        self._settings_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
