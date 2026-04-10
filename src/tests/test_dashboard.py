"""ダッシュボードAPIのテスト"""

from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError


@pytest.fixture
def agent_dir(tmp_path):
    """エージェントのワーキングディレクトリ"""
    d = tmp_path / "agent_project"
    d.mkdir()
    return d


@pytest.fixture
def mock_app(agent_dir):
    from server.app import create_app

    mock_config = MagicMock()
    mock_reader = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    mock_config.get_agent.return_value = AgentInfo(
        id="coder",
        name="coder",
        path=str(agent_dir),
        description="実装担当",
        cli="claude",
        model_tier="standard",
        system_prompt="",
    )

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )

    return app, mock_config, agent_dir


class TestDashboardGet:
    """GET /api/agents/{id}/dashboard"""

    def test_dashboard_md_の内容を返す(self, mock_app):
        app, _, agent_dir = mock_app
        kobito_dir = agent_dir / ".kobito"
        kobito_dir.mkdir()
        (kobito_dir / "dashboard.md").write_text("# coder\nテスト内容", encoding="utf-8")

        with TestClient(app) as client:
            resp = client.get("/api/agents/coder/dashboard")

        assert resp.status_code == 200
        assert resp.json()["content"] == "# coder\nテスト内容"

    def test_ファイル不在時は空文字を返す(self, mock_app):
        app, _, agent_dir = mock_app

        with TestClient(app) as client:
            resp = client.get("/api/agents/coder/dashboard")

        assert resp.status_code == 200
        assert resp.json()["content"] == ""

    def test_存在しないエージェントは404(self, mock_app):
        app, mock_config, _ = mock_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not_found")

        with TestClient(app) as client:
            resp = client.get("/api/agents/not_found/dashboard")

        assert resp.status_code == 404


class TestDashboardPut:
    """PUT /api/agents/{id}/dashboard"""

    def test_dashboard_md_に内容を保存する(self, mock_app):
        app, _, agent_dir = mock_app

        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/coder/dashboard",
                json={"content": "# coder\n更新内容"},
            )

        assert resp.status_code == 200
        saved = (agent_dir / ".kobito" / "dashboard.md").read_text(encoding="utf-8")
        assert saved == "# coder\n更新内容"

    def test_kobito_ディレクトリが存在しない場合は自動作成する(self, mock_app):
        app, _, agent_dir = mock_app
        assert not (agent_dir / ".kobito").exists()

        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/coder/dashboard",
                json={"content": "# 新規作成"},
            )

        assert resp.status_code == 200
        assert (agent_dir / ".kobito").is_dir()
        assert (agent_dir / ".kobito" / "dashboard.md").exists()

    def test_存在しないエージェントは404(self, mock_app):
        app, mock_config, _ = mock_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not_found")

        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/not_found/dashboard",
                json={"content": "内容"},
            )

        assert resp.status_code == 404
