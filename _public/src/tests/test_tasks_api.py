"""タスク管理API + Phase1未テストAPIのテスト"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError


TASK_MD = """\
---
title: テストタスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
---

## 背景

テスト用タスク。
"""


@pytest.fixture
def task_app(tmp_path):
    """タスクAPI用のFastAPIアプリ（実ファイルシステム使用）"""
    from server.app import create_app

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# test", encoding="utf-8")

    # .kobito/meta ディレクトリも準備
    meta_dir = project_dir / ".kobito" / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)

    mock_config = MagicMock()
    mock_config.get_agent.return_value = AgentInfo(
        id="system", name="レプリカ", path=str(project_dir),
        cli="claude", model_tier="deep", system_prompt="",
    )

    mock_reader = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )

    return app, project_dir, mock_config


def _create_task_file(project_dir: Path, task_id: str, content: str = TASK_MD):
    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / f"{task_id}.md").write_text(content, encoding="utf-8")


# ================================================================
# タスク一覧 API
# ================================================================

class TestTaskListAPI:
    def test_タスク一覧が返る(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")
        _create_task_file(project_dir, "task_002")

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks")

        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert "order" in data
        assert len(data["tasks"]) == 2

    def test_タスクなしで空リスト(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks")
        assert resp.status_code == 200
        assert resp.json()["tasks"] == []

    def test_存在しないエージェントで404(self, task_app):
        app, _, mock_config = task_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not found")
        with TestClient(app) as client:
            resp = client.get("/api/agents/nonexistent/tasks")
        assert resp.status_code == 404


# ================================================================
# タスク詳細 API
# ================================================================

class TestTaskDetailAPI:
    def test_タスク詳細が返る(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks/task_001")

        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "task_001"
        assert data["title"] == "テストタスク"

    def test_存在しないタスクで404(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.get("/api/agents/system/tasks/nonexistent")
        assert resp.status_code == 404


# ================================================================
# 承認 API
# ================================================================

class TestApproveAPI:
    def test_承認できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/task_001/approve")

        assert resp.status_code == 200
        assert resp.json()["approval"] == "approved"

    def test_承認後に実行順序に含まれる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            client.post("/api/agents/system/tasks/task_001/approve")
            resp = client.get("/api/agents/system/tasks")

        assert "task_001" in resp.json()["order"]

    def test_存在しないタスクの承認で404(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/nonexistent/approve")
        assert resp.status_code == 404


# ================================================================
# 強制完了 API
# ================================================================

class TestForceDoneAPI:
    def test_強制完了できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/task_001/force-done")

        assert resp.status_code == 200
        assert resp.json()["phase"] == "done"

    def test_存在しないタスクで404(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/nonexistent/force-done")
        assert resp.status_code == 404


# ================================================================
# 削除 API
# ================================================================

class TestDeleteAPI:
    def test_タスクを削除できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.delete("/api/agents/system/tasks/task_001")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert not (project_dir / "tasks" / "task_001.md").exists()


# ================================================================
# 実行順序 API
# ================================================================

class TestOrderAPI:
    def test_順序を更新できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")
        _create_task_file(project_dir, "task_002")

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/tasks/order",
                              json={"order": ["task_002", "task_001"]})

        assert resp.status_code == 200
        assert resp.json()["order"] == ["task_002", "task_001"]

    def test_存在しないタスクIDはフィルタされる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/tasks/order",
                              json={"order": ["task_001", "ghost"]})

        assert resp.json()["order"] == ["task_001"]


# ================================================================
# セッション紐づけ API
# ================================================================

class TestSessionBindingAPI:
    def test_作業セッションを追加できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/task_001/sessions",
                               json={"session_id": "sess-abc"})

        assert resp.status_code == 200
        assert "sess-abc" in resp.json()["sessions"]

    def test_相談セッションを設定できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/tasks/task_001/talk-session",
                              json={"session_id": "talk-001"})

        assert resp.status_code == 200
        assert resp.json()["talk_session_id"] == "talk-001"

    def test_存在しないタスクへのセッション追加で404(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.post("/api/agents/system/tasks/nonexistent/sessions",
                               json={"session_id": "sess-abc"})
        assert resp.status_code == 404


# ================================================================
# タスク本文更新 API
# ================================================================

class TestUpdateBodyAPI:
    def test_本文を更新できる(self, task_app):
        app, project_dir, _ = task_app
        _create_task_file(project_dir, "task_001")

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/tasks/task_001",
                              json={"body": "## 更新後\n\n新しい本文"})

        assert resp.status_code == 200
        assert "更新後" in resp.json()["body"]

    def test_存在しないタスクで404(self, task_app):
        app, _, _ = task_app
        with TestClient(app) as client:
            resp = client.put("/api/agents/system/tasks/nonexistent",
                              json={"body": "本文"})
        assert resp.status_code == 404


# ================================================================
# Phase 1 未テストAPI: title / model-tier / hide
# ================================================================

class TestSessionTitleAPI:
    def test_タイトルを更新できる(self, task_app):
        app, project_dir, _ = task_app

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/sessions/sess-001/title",
                              json={"title": "新しいタイトル"})

        assert resp.status_code == 200
        assert resp.json()["title"] == "新しいタイトル"

        # メタデータファイルに永続化されている
        meta_path = project_dir / ".kobito" / "meta" / "sess-001.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["title"] == "新しいタイトル"

    def test_既存メタデータを保持したまま更新できる(self, task_app):
        app, project_dir, _ = task_app
        meta_dir = project_dir / ".kobito" / "meta"
        meta_path = meta_dir / "sess-001.json"
        meta_path.write_text(json.dumps({"model_tier": "quick"}), encoding="utf-8")

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/sessions/sess-001/title",
                              json={"title": "タイトル"})

        assert resp.status_code == 200
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["title"] == "タイトル"
        assert meta["model_tier"] == "quick"  # 既存フィールド維持


class TestSessionModelTierAPI:
    def test_モデルティアを更新できる(self, task_app):
        app, project_dir, _ = task_app

        with TestClient(app) as client:
            resp = client.put("/api/agents/system/sessions/sess-001/model-tier",
                              json={"model_tier": "quick"})

        assert resp.status_code == 200
        meta_path = project_dir / ".kobito" / "meta" / "sess-001.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["model_tier"] == "quick"

    def test_既存メタデータを保持したまま更新できる(self, task_app):
        app, project_dir, _ = task_app
        meta_dir = project_dir / ".kobito" / "meta"
        meta_path = meta_dir / "sess-001.json"
        meta_path.write_text(json.dumps({"title": "既存タイトル"}), encoding="utf-8")

        with TestClient(app) as client:
            client.put("/api/agents/system/sessions/sess-001/model-tier",
                       json={"model_tier": "deep"})

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["model_tier"] == "deep"
        assert meta["title"] == "既存タイトル"


class TestHideSessionAPI:
    def test_セッションを非表示にできる(self, task_app):
        app, project_dir, _ = task_app

        with TestClient(app) as client:
            resp = client.post("/api/agents/system/sessions/sess-001/hide")

        assert resp.status_code == 200
        meta_path = project_dir / ".kobito" / "meta" / "sess-001.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["hidden"] is True

    def test_既存メタデータを保持したまま非表示にできる(self, task_app):
        app, project_dir, _ = task_app
        meta_dir = project_dir / ".kobito" / "meta"
        meta_path = meta_dir / "sess-001.json"
        meta_path.write_text(json.dumps({"title": "残るタイトル"}), encoding="utf-8")

        with TestClient(app) as client:
            client.post("/api/agents/system/sessions/sess-001/hide")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["hidden"] is True
        assert meta["title"] == "残るタイトル"
