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
    cli: str = "claude"


class SessionReader(ABC):
    @abstractmethod
    def list_sessions(self, project_path: str, cli: str = "claude") -> list[SessionSummary]:
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
        # { jsonl_path_str: (mtime, SessionSummary) } — ファイル変化時のみ再パース
        self._summary_cache: dict[str, tuple[float, SessionSummary | None]] = {}

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

    def _parse_summary(self, jsonl_path: Path, project_path: str) -> SessionSummary | None:
        """1ファイルを全パースしてSummaryを返す（キャッシュなし）"""
        session_id = jsonl_path.stem
        meta = self._load_meta(project_path, session_id)
        if meta.get("hidden"):
            return None
        events = self._parse_jsonl(jsonl_path)
        messages = self._extract_messages(events)
        if not messages:
            return None
        return SessionSummary(
            session_id=session_id,
            created_at=messages[0].timestamp,
            updated_at=messages[-1].timestamp,
            message_count=len(messages),
            last_message=messages[-1].content[:100],
            title=meta.get("title", ""),
            model_tier=meta.get("model_tier", ""),
            initiated_by=meta.get("initiated_by", ""),
            cli=meta.get("cli", "claude"),
        )

    def list_sessions(self, project_path: str, cli: str = "claude") -> list[SessionSummary]:
        sessions_dir = self._sessions_dir(project_path)
        if not sessions_dir.exists():
            return []

        summaries = []
        for jsonl_path in sessions_dir.glob("*.jsonl"):
            mtime = jsonl_path.stat().st_mtime
            key = str(jsonl_path)
            cached = self._summary_cache.get(key)
            if cached and cached[0] == mtime:
                summary = cached[1]
            else:
                summary = self._parse_summary(jsonl_path, project_path)
                self._summary_cache[key] = (mtime, summary)
            if summary:
                summaries.append(summary)

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def read_session(self, project_path: str, session_id: str) -> list[SessionMessage]:
        sessions_dir = self._sessions_dir(project_path)
        jsonl_path = sessions_dir / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            return []

        events = self._parse_jsonl(jsonl_path)
        return self._extract_messages(events)

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


class CodexSessionReader(SessionReader):
    """Codex CLI のセッションを読み取る。

    セッションファイルは ~/.codex/sessions/YYYY/MM/DD/{thread_id}.jsonl に保存される。
    プロジェクトとの対応は .kobito/meta/{session_id}.json の cli フィールドで管理する。

    セッションファイルのイベント形式（codex exec --json の出力形式に準拠）:
      {"type":"thread.started","thread_id":"..."}
      {"type":"item.completed","item":{"type":"user_message","text":"..."}}   # ユーザー発言
      {"type":"turn.started"}
      {"type":"item.completed","item":{"type":"agent_message","text":"..."}}  # アシスタント応答
      {"type":"turn.completed","usage":{...}}
    """

    def __init__(self, codex_home: Path | None = None):
        self._codex_home = codex_home or (Path.home() / ".codex")

    def get_project_hash(self, project_path: str) -> str:
        return ""  # Codex はプロジェクトハッシュを持たない

    def _find_session_file(self, session_id: str) -> Path | None:
        """thread_id からセッションファイルを探す（日付ディレクトリを再帰検索）"""
        sessions_dir = self._codex_home / "sessions"
        if not sessions_dir.exists():
            return None
        for p in sessions_dir.glob(f"**/*{session_id}*.jsonl"):
            return p
        return None

    def _load_meta(self, project_path: str, session_id: str) -> dict:
        meta_path = Path(project_path) / ".kobito" / "meta" / f"{session_id}.json"
        if meta_path.exists():
            return json.loads(meta_path.read_text(encoding="utf-8"))
        return {}

    def _parse_jsonl(self, path: Path) -> list[dict]:
        lines = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return lines

    def _extract_messages(self, events: list[dict]) -> list[SessionMessage]:
        """Codex 保存済み JSONL からメッセージを抽出する。

        保存フォーマット（~/.codex/sessions/**/*.jsonl）:
          {"timestamp":"...","type":"event_msg","payload":{"type":"user_message","message":"..."}}
          {"timestamp":"...","type":"event_msg","payload":{"type":"agent_message","message":"..."}}
        """
        messages = []
        for event in events:
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload", {})
            ptype = payload.get("type", "")
            timestamp = event.get("timestamp", "")
            if ptype == "user_message":
                text = payload.get("message", "")
                if text:
                    messages.append(SessionMessage(role="user", content=text, timestamp=timestamp))
            elif ptype == "agent_message":
                text = payload.get("message", "")
                if text:
                    messages.append(SessionMessage(role="assistant", content=text, timestamp=timestamp))
        return messages

    def list_sessions(self, project_path: str, cli: str = "claude") -> list[SessionSummary]:
        """kobito meta から cli=="codex" のセッションを収集する"""
        meta_dir = Path(project_path) / ".kobito" / "meta"
        if not meta_dir.exists():
            return []

        summaries = []
        for meta_path in meta_dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("cli") != "codex" or meta.get("hidden"):
                continue

            session_id = meta_path.stem
            jsonl_path = self._find_session_file(session_id)
            if not jsonl_path:
                continue

            events = self._parse_jsonl(jsonl_path)
            messages = self._extract_messages(events)
            if not messages:
                continue

            mtime_ts = jsonl_path.stat().st_mtime
            import datetime as _dt
            updated_at = _dt.datetime.fromtimestamp(mtime_ts).isoformat()
            summaries.append(SessionSummary(
                session_id=session_id,
                created_at=updated_at,
                updated_at=updated_at,
                message_count=len(messages),
                last_message=messages[-1].content[:100],
                title=meta.get("title", ""),
                model_tier=meta.get("model_tier", ""),
                initiated_by=meta.get("initiated_by", ""),
                cli="codex",
            ))

        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def read_session(self, project_path: str, session_id: str) -> list[SessionMessage]:
        jsonl_path = self._find_session_file(session_id)
        if not jsonl_path:
            return []
        events = self._parse_jsonl(jsonl_path)
        return self._extract_messages(events)

    def get_dir_mtime(self, project_path: str) -> float:
        """kobito meta から Codex セッションファイルの最大更新時刻を返す"""
        meta_dir = Path(project_path) / ".kobito" / "meta"
        if not meta_dir.exists():
            return 0
        max_mtime = 0.0
        for meta_path in meta_dir.glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if meta.get("cli") != "codex":
                continue
            f = self._find_session_file(meta_path.stem)
            if f:
                mt = f.stat().st_mtime
                if mt > max_mtime:
                    max_mtime = mt
        return max_mtime

    def get_session_mtime(self, project_path: str, session_id: str) -> float:
        f = self._find_session_file(session_id)
        return f.stat().st_mtime if f else 0


class AgentSessionReader:
    """エージェントの cli 種別に応じて適切な SessionReader へ委譲する"""

    def __init__(self, claude_home: "Path | None" = None):
        self._readers: dict[str, SessionReader] = {
            "claude": ClaudeSessionReader(claude_home=claude_home),
            "codex":  CodexSessionReader(),
        }

    def _get(self, cli: str) -> SessionReader:
        return self._readers.get(cli, self._readers["claude"])

    def _cli_from_meta(self, project_path: str, session_id: str, fallback: str) -> str:
        """セッションメタから実際に使われた CLI を返す。メタがなければ fallback を使う"""
        meta_path = Path(project_path) / ".kobito" / "meta" / f"{session_id}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return meta.get("cli", fallback)
            except (json.JSONDecodeError, OSError):
                pass
        return fallback

    def list_sessions(self, project_path: str, cli: str = "claude") -> list[SessionSummary]:
        """CLI に関わらず全セッションを返す（Claude + Codex を混在表示するため）"""
        claude = self._readers["claude"].list_sessions(project_path)
        codex = self._readers["codex"].list_sessions(project_path)
        combined = claude + codex
        combined.sort(key=lambda s: s.updated_at, reverse=True)
        return combined

    def read_session(self, project_path: str, session_id: str, cli: str = "claude") -> list[SessionMessage]:
        """セッションを作った CLI（メタ記録）で読む。エージェントの現在の CLI に依存しない"""
        actual_cli = self._cli_from_meta(project_path, session_id, fallback=cli)
        return self._get(actual_cli).read_session(project_path, session_id)

    def get_dir_mtime(self, project_path: str, cli: str = "claude") -> float:
        """両 CLI の最大 mtime を返す"""
        claude_mtime = self._readers["claude"].get_dir_mtime(project_path)  # type: ignore[attr-defined]
        codex_mtime = self._readers["codex"].get_dir_mtime(project_path)  # type: ignore[attr-defined]
        return max(claude_mtime, codex_mtime)

    def get_session_mtime(self, project_path: str, session_id: str, cli: str = "claude") -> float:
        actual_cli = self._cli_from_meta(project_path, session_id, fallback=cli)
        r = self._get(actual_cli)
        return r.get_session_mtime(project_path, session_id) if hasattr(r, "get_session_mtime") else 0  # type: ignore[attr-defined]

