"""チームセッション管理"""

from __future__ import annotations

import json
import random
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class TeamSessionNotFoundError(Exception):
    """指定されたチームセッションが存在しない"""


@dataclass
class TeamSession:
    session_id: str
    team_id: str
    title: str
    created_at: str
    messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "team_id": self.team_id,
            "title": self.title,
            "created_at": self.created_at,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TeamSession":
        return cls(
            session_id=data["session_id"],
            team_id=data["team_id"],
            title=data["title"],
            created_at=data["created_at"],
            messages=data.get("messages", []),
        )


class TeamSessionManager:
    def __init__(self, data_dir: Path | str):
        self._data_dir = Path(data_dir)

    def _session_dir(self, team_id: str) -> Path:
        return self._data_dir / "team_sessions" / team_id

    def _session_path(self, team_id: str, session_id: str) -> Path:
        return self._session_dir(team_id) / f"{session_id}.json"

    def _generate_session_id(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"ts_{ts}_{suffix}"

    def _save(self, session: TeamSession) -> None:
        path = self._session_path(session.team_id, session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create_session(self, team_id: str, title: str) -> TeamSession:
        """新規チームセッションを作成してファイルに保存する"""
        session = TeamSession(
            session_id=self._generate_session_id(),
            team_id=team_id,
            title=title,
            created_at=datetime.now().isoformat(),
        )
        self._save(session)
        return session

    def load_session(self, team_id: str, session_id: str) -> TeamSession:
        """セッションをファイルから読み込む"""
        path = self._session_path(team_id, session_id)
        if not path.exists():
            raise TeamSessionNotFoundError(
                f"セッション '{session_id}' が見つかりません (team: {team_id})"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return TeamSession.from_dict(data)

    def list_sessions(self, team_id: str) -> list[TeamSession]:
        """チームのセッション一覧を返す"""
        session_dir = self._session_dir(team_id)
        if not session_dir.exists():
            return []
        sessions = []
        for p in session_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                sessions.append(TeamSession.from_dict(data))
            except (json.JSONDecodeError, KeyError):
                continue
        return sessions

    def update_title(self, team_id: str, session_id: str, title: str) -> TeamSession:
        """セッションのタイトルを更新して永続化する"""
        session = self.load_session(team_id, session_id)
        session.title = title
        self._save(session)
        return session

    def append_message(self, team_id: str, session_id: str, message: dict) -> TeamSession:
        """セッションにメッセージを追加して永続化する"""
        session = self.load_session(team_id, session_id)
        session.messages.append(message)
        self._save(session)
        return session
