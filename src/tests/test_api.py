"""Web APIのテスト"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError
from server.session_reader import SessionSummary, SessionMessage


@pytest.fixture
def mock_app():
    """モック依存でFastAPIアプリを作成"""
    from server.app import create_app

    mock_config = MagicMock()
    mock_reader = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )

    return app, mock_config, mock_reader, mock_bridge


class TestAgentsAPI:
    """エージェント関連API"""

    def test_エージェント一覧が返る(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.list_agents.return_value = [
            AgentInfo(id="system", name="レプリカ", path="/tmp", description="テスト",
                      cli="claude", model_tier="deep", system_prompt=""),
        ]

        with TestClient(app) as client:
            resp = client.get("/api/agents")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "system"

    def test_エージェント詳細が返る(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp", description="テスト",
            cli="claude", model_tier="deep", system_prompt="",
        )

        with TestClient(app) as client:
            resp = client.get("/api/agents/system")

        assert resp.status_code == 200
        assert resp.json()["name"] == "レプリカ"

    def test_存在しないエージェントで404(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not found")

        with TestClient(app) as client:
            resp = client.get("/api/agents/nonexistent")

        assert resp.status_code == 404

    def test_エージェント設定を更新できる(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.update_agent.return_value = AgentInfo(
            id="system", name="新名前", path="/tmp", description="新説明",
            cli="claude", model_tier="quick", system_prompt="",
        )

        with TestClient(app) as client:
            resp = client.put("/api/agents/system", json={
                "name": "新名前", "description": "新説明", "model_tier": "quick",
            })

        assert resp.status_code == 200
        assert resp.json()["name"] == "新名前"


class TestSystemPromptAPI:
    """CLAUDE.md関連API"""

    def test_CLAUDE_mdが取得できる(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.get_system_prompt.return_value = "# テストプロンプト"

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/system-prompt")

        assert resp.status_code == 200
        assert resp.json()["content"] == "# テストプロンプト"

    def test_CLAUDE_mdを更新できる(self, mock_app):
        app, mock_config, _, _ = mock_app

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/system-prompt", json={
                "content": "# 更新後",
            })

        assert resp.status_code == 200
        mock_config.update_system_prompt.assert_called_once_with("system", "# 更新後")


class TestSessionsAPI:
    """セッション関連API"""

    def test_セッション一覧が返る(self, mock_app):
        app, mock_config, mock_reader, _ = mock_app
        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp/project", cli="claude", model_tier="deep", system_prompt="",
        )
        mock_reader.list_sessions.return_value = [
            SessionSummary(session_id="sess-001", created_at="2026-04-01T06:00:00Z",
                           updated_at="2026-04-01T06:01:00Z", message_count=4,
                           last_message="最後のメッセージ"),
        ]

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/sessions")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["session_id"] == "sess-001"
        assert data[0]["message_count"] == 4

    def test_セッション履歴が返る(self, mock_app):
        app, mock_config, mock_reader, _ = mock_app
        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp/project", cli="claude", model_tier="deep", system_prompt="",
        )
        mock_reader.read_session.return_value = [
            SessionMessage(role="user", content="こんにちは", timestamp="2026-04-01T06:00:00Z", tool_uses=[]),
            SessionMessage(role="assistant", content="やあ", timestamp="2026-04-01T06:00:05Z", tool_uses=[]),
        ]

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/sessions/sess-001")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["role"] == "user"

    def test_チャット送信でSSEストリームが返る(self, mock_app):
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp/project", cli="claude",
            model_tier="deep", system_prompt="テスト",
        )

        async def fake_stream(*args, **kwargs):
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "応答"}]}}
            yield {"type": "result", "session_id": "sess-new", "result": "応答"}

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/chat", json={
                "message": "こんにちは",
            })

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_CLI起動エンドポイント(self, mock_app):
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = AgentInfo(
            id="system", name="レプリカ", path="/tmp/project", cli="claude", model_tier="deep", system_prompt="",
        )

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/cli", json={"session_id": "sess-001"})

        assert resp.status_code == 200
