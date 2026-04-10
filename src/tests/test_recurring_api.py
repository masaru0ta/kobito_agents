"""定期タスク設定 API テスト（Phase10）"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo

TASK_MD = """\
---
id: task_001
title: テスト定期タスク
agent: system
---

- [ ] ステップ1
- [ ] ステップ2
"""


@pytest.fixture
def task_app(tmp_path):
    from server.app import create_app

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# test", encoding="utf-8")
    (project_dir / ".kobito" / "meta").mkdir(parents=True, exist_ok=True)

    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task_001.md").write_text(TASK_MD, encoding="utf-8")

    mock_config = MagicMock()
    mock_config.get_agent.return_value = AgentInfo(
        id="system", name="レプリカ", path=str(project_dir),
        cli="claude", model_tier="deep", system_prompt="",
    )
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=MagicMock(),
        cli_bridge=mock_bridge,
    )
    return app, project_dir


class TestRecurringAPI:

    def test_get_recurring_not_set(self, task_app):
        """未設定時は is_recurring=False を返す"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks/task_001/recurring")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_recurring"] is False
        assert data["reset_interval"] is None

    def test_put_recurring_daily(self, task_app):
        """daily設定を保存できる"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "daily", "reset_time": "09:00"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_recurring"] is True
        assert data["reset_interval"] == "daily"
        assert data["reset_time"] == "09:00"

    def test_put_recurring_weekly(self, task_app):
        """weekly設定を保存できる"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "weekly", "reset_weekday": "monday", "reset_time": "09:00"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reset_interval"] == "weekly"
        assert data["reset_weekday"] == "monday"

    def test_put_recurring_monthly(self, task_app):
        """monthly設定を保存できる"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "monthly", "reset_monthday": 1, "reset_time": "09:00"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reset_interval"] == "monthly"
        assert data["reset_monthday"] == 1

    def test_put_recurring_disabled(self, task_app):
        """repeat_enabled=False で一時停止できる"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "daily", "reset_time": "09:00", "repeat_enabled": False},
            )
        assert resp.status_code == 200
        assert resp.json()["repeat_enabled"] is False

    def test_get_recurring_after_put(self, task_app):
        """PUT後にGETで設定が取得できる"""
        app, _ = task_app
        with TestClient(app) as client:
            client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "daily", "reset_time": "18:00"},
            )
            resp = client.get("/api/agents/system/tasks/task_001/recurring")
        assert resp.status_code == 200
        assert resp.json()["reset_time"] == "18:00"

    def test_delete_recurring(self, task_app):
        """DELETE で定期設定を解除できる"""
        app, _ = task_app
        with TestClient(app) as client:
            client.put(
                "/api/agents/system/tasks/task_001/recurring",
                json={"reset_interval": "daily", "reset_time": "09:00"},
            )
            resp = client.delete("/api/agents/system/tasks/task_001/recurring")
        assert resp.status_code == 200
        assert resp.json()["is_recurring"] is False

    def test_get_recurring_task_not_found(self, task_app):
        """存在しないタスクは404"""
        app, _ = task_app
        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks/no_such_task/recurring")
        assert resp.status_code == 404
