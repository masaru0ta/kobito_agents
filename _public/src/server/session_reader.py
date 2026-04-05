"""SessionReader — CLIツールのセッションデータ読み取り（アダプターパターン）"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel


class SessionMessage(BaseModel):
    role: str
    content: str
    timestamp: str
    tool_uses: list[dict] = []


class SessionSummary(BaseModel):
    session_id: str
    created_at: str
    updated_at: str
    message_count: int
    last_message: str
    title: str = ""
    model_tier: str = ""
    initiated_by: str = ""


class SessionReader(ABC):
    @abstractmethod
    def list_sessions(self, project_path: str) -> list[SessionSummary]:
        ...

    @abstractmethod
    def read_session(self, project_path: str, session_id: str) -> list[SessionMessage]:
        ...

    @abstractmethod
    def get_project_hash(self, project_path: str) -> str:
        ...


class ClaudeSessionReader(SessionReader):
    def __init__(self, claude_home: Path | None = None):
        if claude_home is None:
            claude_home = Path.home() / ".claude"
        self._claude_home = claude_home

    def get_project_hash(self, project_path: str) -> str:
        """プロジェクトパスからClaude Codeのproject_hashを算出する"""
        return project_path.replace("\\", "-").replace(":", "-").replace("/", "-").replace("_", "-")

    def _sessions_dir(self, project_path: str) -> Path:
        return self._claude_home / "projects" / self.get_project_hash(project_path)

    def get_dir_mtime(self, project_path: str) -> float:
        """セッションディレクトリ内のJSONLファイル群の最大更新時刻を返す"""
        sessions_dir = self._sessions_dir(project_path)
        if not sessions_dir.exists():
            return 0
        max_mtime = 0
        for p in sessions_dir.glob("*.jsonl"):
            mt = p.stat().st_mtime
            if mt > max_mtime:
                max_mtime = mt
        return max_mtime

    def get_session_mtime(self, project_path: str, session_id: str) -> float:
        """指定セッションJSONLの更新時刻を返す（存在しなければ0）"""
        p = self._sessions_dir(project_path) / f"{session_id}.jsonl"
        return p.stat().st_mtime if p.exists() else 0

    def _load_meta(self, project_path: str, session_id: str) -> dict:
        """`.kobito/meta/{session_id}.json` を読む。存在しなければ空dictを返す"""
        meta_path = Path(project_path) / ".kobito" / "meta" / f"{session_id}.json"
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
        return {}

    def _parse_jsonl(self, path: Path) -> list[dict]:
        """JSONLファイルをパースして全行を返す"""
        lines = []
        text = path.read_text(encoding="utf-8")
        for line in text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return lines

    def _extract_messages(self, events: list[dict]) -> list[SessionMessage]:
        """JSONL行からuser/assistantメッセージを抽出する"""
        messages = []
        for event in events:
            etype = event.get("type", "")
            if etype not in ("user", "assistant"):
                continue

            msg = event.get("message", {})
            timestamp = event.get("timestamp", "")

            if etype == "user":
                content = msg.get("content", "")
                # contentがリストの場合（tool_result等）はテキスト部分を結合
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            parts.append(item.get("text", ""))
                        elif isinstance(item, str):
                            parts.append(item)
                    content = "\n".join(parts) if parts else ""
                # コンパクションサマリーをスキップ（isCompactSummaryフラグ or 文字列マッチング）
                if event.get("isCompactSummary"):
                    continue
                if isinstance(content, str) and content.lstrip().startswith(
                    "This session is being continued from a previous conversation"
                ):
                    continue
                messages.append(SessionMessage(
                    role="user",
                    content=content,
                    timestamp=timestamp,
                ))
            elif etype == "assistant":
                text = ""
                tool_uses = []
                for item in msg.get("content", []):
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text = item.get("text", "")
                        elif item.get("type") == "tool_use":
                            tool_uses.append(item)
                messages.append(SessionMessage(
                    role="assistant",
                    content=text,
                    timestamp=timestamp,
                    tool_uses=tool_uses,
                ))
        return messages

    def list_sessions(self, project_path: str) -> list[SessionSummary]:
        sessions_dir = self._sessions_dir(project_path)
        if not sessions_dir.exists():
            return []

        summaries = []
        for jsonl_path in sessions_dir.glob("*.jsonl"):
            session_id = jsonl_path.stem

            # 非表示チェック
            meta = self._load_meta(project_path, session_id)
            if meta.get("hidden"):
                continue

            events = self._parse_jsonl(jsonl_path)
            messages = self._extract_messages(events)

            if not messages:
                continue

            summaries.append(SessionSummary(
                session_id=session_id,
                created_at=messages[0].timestamp,
                updated_at=messages[-1].timestamp,
                message_count=len(messages),
                last_message=messages[-1].content[:100],
                title=meta.get("title", ""),
                model_tier=meta.get("model_tier", ""),
                initiated_by=meta.get("initiated_by", ""),
            ))

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def read_session(self, project_path: str, session_id: str) -> list[SessionMessage]:
        sessions_dir = self._sessions_dir(project_path)
        jsonl_path = sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return []

        events = self._parse_jsonl(jsonl_path)
        return self._extract_messages(events)

