"""ChatManagerのテスト"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_session_jsonl


def _user_message(uuid, content, timestamp, session_id="sess-001"):
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": content},
        "timestamp": timestamp,
        "sessionId": session_id,
    }


def _assistant_message(uuid, content, timestamp, session_id="sess-001"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": [{"type": "text", "text": content}]},
        "timestamp": timestamp,
        "sessionId": session_id,
    }


class TestChatManagerSessions:
    """セッション一覧・履歴取得"""

    def test_セッション一覧がSessionReader経由で取得できる(self):
        from server.chat import ChatManager

        mock_reader = MagicMock()
        mock_reader.list_sessions.return_value = [
            MagicMock(session_id="sess-001", message_count=4),
            MagicMock(session_id="sess-002", message_count=2),
        ]
        mock_config = MagicMock()
        mock_config.get_agent.return_value = MagicMock(path="/tmp/project")

        cm = ChatManager(config_manager=mock_config, session_reader=mock_reader, cli_bridge=MagicMock())
        sessions = cm.list_sessions("system")

        mock_reader.list_sessions.assert_called_once_with("/tmp/project")
        assert len(sessions) == 2

    def test_セッション履歴がSessionReader経由で取得できる(self):
        from server.chat import ChatManager

        mock_reader = MagicMock()
        mock_reader.read_session.return_value = [
            MagicMock(role="user", content="こんにちは"),
            MagicMock(role="assistant", content="やあ"),
        ]
        mock_config = MagicMock()
        mock_config.get_agent.return_value = MagicMock(path="/tmp/project")

        cm = ChatManager(config_manager=mock_config, session_reader=mock_reader, cli_bridge=MagicMock())
        messages = cm.get_session_history("system", "sess-001")

        mock_reader.read_session.assert_called_once_with("/tmp/project", "sess-001")
        assert len(messages) == 2


class TestChatManagerHide:
    """セッションの非表示"""

    def test_セッションを非表示にできる(self, tmp_project_dir):
        from server.chat import ChatManager

        mock_config = MagicMock()
        mock_config.get_agent.return_value = MagicMock(path=str(tmp_project_dir))

        cm = ChatManager(config_manager=mock_config, session_reader=MagicMock(), cli_bridge=MagicMock())
        cm.hide_session("system", "sess-001")

        meta_path = tmp_project_dir / ".kobito" / "meta" / "sess-001.json"
        assert meta_path.exists()

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["hidden"] is True


class TestChatManagerSummarize:
    """会話要約"""

    def test_要約を生成しメタデータに保存できる(self, tmp_project_dir):
        from server.chat import ChatManager

        mock_config = MagicMock()
        mock_config.get_agent.return_value = MagicMock(path=str(tmp_project_dir))

        mock_reader = MagicMock()
        mock_reader.read_session.return_value = [
            MagicMock(role="user", content="Phase 1の設計について"),
            MagicMock(role="assistant", content="Phase 1ではWebUIとチャット機能を実装する"),
        ]

        mock_bridge = MagicMock()

        async def fake_summarize(*args, **kwargs):
            return {"title": "Phase 1設計", "summary": "WebUIとチャットの実装方針"}

        mock_bridge.summarize = AsyncMock(side_effect=fake_summarize)

        cm = ChatManager(config_manager=mock_config, session_reader=mock_reader, cli_bridge=mock_bridge)

        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            cm.summarize_session("system", "sess-001")
        )

        assert result["title"] == "Phase 1設計"

        meta_path = tmp_project_dir / ".kobito" / "meta" / "sess-001.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["title"] == "Phase 1設計"
