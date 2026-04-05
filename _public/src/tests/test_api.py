"""Web APIのテスト"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError, DuplicatePathError, SystemAgentProtectedError
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


class TestAgentsAddAPI:
    """POST /api/agents — エージェント追加"""

    def test_エージェントを追加できる(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.add_agent.return_value = AgentInfo(
            id="agent_20260404_143000_x7k", name="キャスパー", path="/tmp/game",
            description="ゲームデザイナー", cli="claude", model_tier="deep", system_prompt="",
        )

        with TestClient(app) as client:
            resp = client.post("/api/agents", json={
                "name": "キャスパー",
                "path": "/tmp/game",
                "description": "ゲームデザイナー",
                "cli": "claude",
                "model_tier": "deep",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "agent_20260404_143000_x7k"
        assert data["name"] == "キャスパー"
        mock_config.add_agent.assert_called_once_with(
            name="キャスパー", path="/tmp/game", description="ゲームデザイナー",
            cli="claude", model_tier="deep",
        )

    def test_バリデーションエラーで400(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.add_agent.side_effect = ValueError("name は空にできません")

        with TestClient(app) as client:
            resp = client.post("/api/agents", json={
                "name": "",
                "path": "/tmp/game",
                "description": "",
                "cli": "claude",
                "model_tier": "deep",
            })

        assert resp.status_code == 400

    def test_重複pathで409(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.add_agent.side_effect = DuplicatePathError("パス '/tmp' は既に登録されています")

        with TestClient(app) as client:
            resp = client.post("/api/agents", json={
                "name": "重複",
                "path": "/tmp",
                "description": "",
                "cli": "claude",
                "model_tier": "deep",
            })

        assert resp.status_code == 409


class TestAgentsDeleteAPI:
    """DELETE /api/agents/{id} — エージェント削除"""

    def test_エージェントを削除できる(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.delete_agent.return_value = None

        with TestClient(app) as client:
            resp = client.delete("/api/agents/agent_20260404_143000_x7k")

        assert resp.status_code == 200
        mock_config.delete_agent.assert_called_once_with("agent_20260404_143000_x7k")

    def test_systemエージェント削除で403(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.delete_agent.side_effect = SystemAgentProtectedError("systemエージェントは削除できません")

        with TestClient(app) as client:
            resp = client.delete("/api/agents/system")

        assert resp.status_code == 403

    def test_存在しないエージェント削除で404(self, mock_app):
        app, mock_config, _, _ = mock_app
        mock_config.delete_agent.side_effect = AgentNotFoundError("not found")

        with TestClient(app) as client:
            resp = client.delete("/api/agents/nonexistent")

        assert resp.status_code == 404
