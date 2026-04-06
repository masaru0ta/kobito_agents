"""セッションメタ拡張 — initiated_by フィールドのテスト"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError


def _make_agent(agent_id="agent_b", name="テストB", path=None) -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        name=name,
        path=path or "/tmp/agent_b",
        cli="claude",
        model_tier="deep",
    )


@pytest.fixture
def tmp_agent_dir(tmp_path):
    """エージェントの作業ディレクトリ"""
    d = tmp_path / "agent_b"
    d.mkdir()
    return d


@pytest.fixture
def mock_app(tmp_agent_dir):
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
    return app, mock_config, mock_reader, mock_bridge, tmp_agent_dir


def _fake_stream(session_id="sess-new"):
    async def _gen(*a, **kw):
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "回答"}]},
        }
        yield {"type": "result", "session_id": session_id, "result": "回答"}
    return _gen()


class TestInitiatedByRecording:
    """ask_agent 経由のセッションに initiated_by が記録される"""

    def test_新規セッションにinitiated_byが書き込まれる(self, mock_app):
        app, mock_config, _, mock_bridge, agent_dir = mock_app
        mock_config.get_agent.return_value = _make_agent(path=str(agent_dir))
        mock_bridge.run_stream = MagicMock(return_value=_fake_stream("sess-001"))

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
            })

        assert resp.status_code == 200
        meta_path = agent_dir / ".kobito" / "meta" / "sess-001.json"
        assert meta_path.exists(), "セッションメタファイルが作成されていない"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "initiated_by" in meta

    def test_initiated_byにcall_chainの先頭が入る(self, mock_app):
        app, mock_config, _, mock_bridge, agent_dir = mock_app
        mock_config.get_agent.side_effect = lambda aid: _make_agent(
            agent_id=aid, name=f"{aid}_name", path=str(agent_dir),
        )
        mock_bridge.run_stream = MagicMock(return_value=_fake_stream("sess-002"))

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
                "call_chain": ["agent_a"],
            })

        assert resp.status_code == 200
        meta_path = agent_dir / ".kobito" / "meta" / "sess-002.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["initiated_by"] == "agent_a"

    def test_call_chain省略時はsystemが入る(self, mock_app):
        app, mock_config, _, mock_bridge, agent_dir = mock_app
        mock_config.get_agent.return_value = _make_agent(path=str(agent_dir))
        mock_bridge.run_stream = MagicMock(return_value=_fake_stream("sess-003"))

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
            })

        assert resp.status_code == 200
        meta_path = agent_dir / ".kobito" / "meta" / "sess-003.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["initiated_by"] == "system"

    def test_既存セッション再開時はメタを上書きしない(self, mock_app):
        """session_id 指定（resume）では initiated_by を書き込まない"""
        app, mock_config, _, mock_bridge, agent_dir = mock_app
        mock_config.get_agent.return_value = _make_agent(path=str(agent_dir))
        mock_bridge.run_stream = MagicMock(return_value=_fake_stream("sess-existing"))

        # 事前にメタを作っておく
        meta_dir = agent_dir / ".kobito" / "meta"
        meta_dir.mkdir(parents=True)
        meta_path = meta_dir / "sess-existing.json"
        meta_path.write_text(json.dumps({"title": "既存タイトル"}), encoding="utf-8")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "続きの質問",
                "session_id": "sess-existing",
            })

        assert resp.status_code == 200
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # 既存メタが保持され、initiated_by が追記されていない
        assert meta.get("title") == "既存タイトル"
        assert "initiated_by" not in meta


class TestInitiatedByInSessionList:
    """セッション一覧 API が initiated_by を含むメタを返す"""

    def test_セッションメタAPIがinitiated_byを返す(self, mock_app):
        app, mock_config, _, _, agent_dir = mock_app
        mock_config.get_agent.return_value = _make_agent(path=str(agent_dir))

        # メタファイルを直接作成
        meta_dir = agent_dir / ".kobito" / "meta"
        meta_dir.mkdir(parents=True)
        meta_path = meta_dir / "sess-with-meta.json"
        meta_path.write_text(
            json.dumps({"initiated_by": "agent_a", "title": "Aからの質問"}),
            encoding="utf-8",
        )

        with TestClient(app) as client:
            resp = client.get("/api/agents/agent_b/sessions/sess-with-meta/meta")

        assert resp.status_code == 200
        data = resp.json()
        assert data["initiated_by"] == "agent_a"
        assert data["title"] == "Aからの質問"
