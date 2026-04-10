"""ConfigManager — エージェント登録情報の管理"""

from __future__ import annotations

import json
import random
import string
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class AgentInfo(BaseModel):
    id: str
    name: str
    path: str = ""
    description: str = ""
    cli: str = "claude"
    model_tier: str = "quick"
    system_prompt: str = ""
    thumbnail_url: str | None = None
    type: str = "agent"
    members: list[str] = []


class AgentNotFoundError(Exception):
    pass


class DuplicatePathError(Exception):
    pass


class SystemAgentProtectedError(Exception):
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
                "model_tier": "quick",
            }
        ]
        self._write_agents(agents)

    def _read_agents(self) -> list[dict]:
        return json.loads(self._agents_file.read_text(encoding="utf-8"))

    def _write_agents(self, agents: list[dict]) -> None:
        self._agents_file.write_text(
            json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    _THUMBNAIL_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def _system_prompt_file(self, path: str, cli: str = "claude") -> Path:
        filename = "AGENTS.md" if cli == "codex" else "CLAUDE.md"
        return Path(path) / filename

    def _read_system_prompt(self, path: str, cli: str = "claude") -> str:
        f = self._system_prompt_file(path, cli)
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def get_thumbnail_path(self, agent_id: str) -> Path | None:
        thumb_dir = self._data_dir / "thumbnails"
        for ext in self._THUMBNAIL_EXTS:
            p = thumb_dir / f"{agent_id}{ext}"
            if p.exists():
                return p
        return None

    def get_thumbnail_url(self, agent_id: str) -> str | None:
        p = self.get_thumbnail_path(agent_id)
        if p:
            v = int(p.stat().st_mtime)
            return f"/api/agents/{agent_id}/thumbnail?v={v}"
        return None

    def save_thumbnail(self, agent_id: str, data: bytes, ext: str) -> None:
        self.delete_thumbnail(agent_id)
        thumb_dir = self._data_dir / "thumbnails"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        (thumb_dir / f"{agent_id}{ext}").write_bytes(data)

    def delete_thumbnail(self, agent_id: str) -> bool:
        p = self.get_thumbnail_path(agent_id)
        if p:
            p.unlink()
            return True
        return False

    def _agent_system_prompt(self, agent: dict) -> str:
        path = agent.get("path", "")
        if not path:
            return ""
        return self._read_system_prompt(path, agent.get("cli", "claude"))

    def list_agents(self) -> list[AgentInfo]:
        agents = self._read_agents()
        return [
            AgentInfo(
                **agent,
                system_prompt=self._agent_system_prompt(agent),
                thumbnail_url=self.get_thumbnail_url(agent["id"]),
            )
            for agent in agents
        ]

    def get_agent(self, agent_id: str) -> AgentInfo:
        for agent in self._read_agents():
            if agent["id"] == agent_id:
                return AgentInfo(
                    **agent,
                    system_prompt=self._agent_system_prompt(agent),
                    thumbnail_url=self.get_thumbnail_url(agent_id),
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
                    system_prompt=self._agent_system_prompt(agent),
                )
        raise AgentNotFoundError(f"エージェント '{agent_id}' が見つかりません")

    def _generate_agent_id(self) -> str:
        """agent_{YYYYMMDDHHmmss}_{ランダム3文字} 形式のIDを生成"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))
        return f"agent_{ts}_{suffix}"

    def _generate_team_id(self) -> str:
        """team_{YYYYMMDDHHmmss}_{ランダム3文字} 形式のIDを生成"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=3))
        return f"team_{ts}_{suffix}"

    def add_agent(
        self, name: str, path: str, description: str, cli: str, model_tier: str
    ) -> AgentInfo:
        """エージェントを新規登録する"""
        # バリデーション
        if not name:
            raise ValueError("name は空にできません")
        if not path or not Path(path).is_dir():
            raise ValueError("path は実在するディレクトリを指定してください")
        if cli not in ("claude", "codex"):
            raise ValueError("cli は 'claude' または 'codex' を指定してください")
        if model_tier not in ("deep", "quick"):
            raise ValueError("model_tier は 'deep' または 'quick' を指定してください")

        # 同一path重複チェック
        agents = self._read_agents()
        for agent in agents:
            if agent["path"] == path:
                raise DuplicatePathError(f"パス '{path}' は既に登録されています")

        new_agent = {
            "id": self._generate_agent_id(),
            "name": name,
            "path": path,
            "description": description,
            "cli": cli,
            "model_tier": model_tier,
        }
        agents.append(new_agent)
        self._write_agents(agents)

        return AgentInfo(
            **new_agent,
            system_prompt=self._read_system_prompt(path, cli),
        )

    def add_team(self, name: str, description: str, members: list[str]) -> AgentInfo:
        """チームエージェントを新規登録する"""
        if not name:
            raise ValueError("name は空にできません")
        if not members:
            raise ValueError("members は空にできません")

        new_team = {
            "id": self._generate_team_id(),
            "name": name,
            "path": "",
            "description": description,
            "type": "team",
            "members": members,
        }
        agents = self._read_agents()
        agents.append(new_team)
        self._write_agents(agents)

        return AgentInfo(**new_team)

    def delete_agent(self, agent_id: str) -> None:
        """エージェントの登録を解除する（systemは削除不可）"""
        if agent_id == "system":
            raise SystemAgentProtectedError("systemエージェントは削除できません")

        agents = self._read_agents()
        new_agents = [a for a in agents if a["id"] != agent_id]

        if len(new_agents) == len(agents):
            raise AgentNotFoundError(f"エージェント '{agent_id}' が見つかりません")

        self._write_agents(new_agents)

    def get_system_prompt(self, agent_id: str) -> str:
        agent = self.get_agent(agent_id)
        return agent.system_prompt

    def update_system_prompt(self, agent_id: str, content: str) -> None:
        agent = self.get_agent(agent_id)
        self._system_prompt_file(agent.path, agent.cli).write_text(content, encoding="utf-8")

    _SETTING_DEFAULTS: dict = {
        "lmstudio_url": "http://localhost:1234/v1",
        "team_max_turns": 20,
    }

    def get_setting(self, key: str, default=None):
        resolved_default = self._SETTING_DEFAULTS.get(key, default)
        if not self._settings_file.exists():
            return resolved_default
        data = json.loads(self._settings_file.read_text(encoding="utf-8"))
        return data.get(key, resolved_default)

    def set_setting(self, key: str, value) -> None:
        data = {}
        if self._settings_file.exists():
            data = json.loads(self._settings_file.read_text(encoding="utf-8"))
        data[key] = value
        self._settings_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
