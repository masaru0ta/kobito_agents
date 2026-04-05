"""内部API POST /api/internal/ask のテスト"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError


def _make_agent(agent_id="agent_b", name="テストB", path="/tmp/b") -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        name=name,
        path=path,
        cli="claude",
        model_tier="deep",
    )


@pytest.fixture
def mock_app():
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


class TestInternalAskAPI:
    """POST /api/internal/ask"""

    def test_正常系_新規セッション(self, mock_app):
        """エージェントに質問して回答を得る"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent()

        async def fake_stream(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "回答だ"}]},
            }
            yield {
                "type": "result",
                "session_id": "sess-new-001",
                "result": "回答だ",
            }

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問です",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent_b"
        assert data["agent_name"] == "テストB"
        assert data["session_id"] == "sess-new-001"
        assert "回答" in data["response"]

    def test_正常系_既存セッション継続(self, mock_app):
        """session_id を指定して既存セッションを再開"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent()

        async def fake_stream(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "続きの回答"}]},
            }
            yield {
                "type": "result",
                "session_id": "sess-existing",
                "result": "続きの回答",
            }

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "続きの質問",
                "session_id": "sess-existing",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-existing"
        # run_stream に session_id が渡されていること
        call_kwargs = mock_bridge.run_stream.call_args
        assert call_kwargs[1].get("session_id") == "sess-existing" or \
               (len(call_kwargs[0]) >= 4 and call_kwargs[0][3] == "sess-existing")

    def test_異常系_存在しないエージェント_404(self, mock_app):
        """存在しない agent_id を指定すると 404"""
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.side_effect = AgentNotFoundError("not found")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "nonexistent",
                "message": "質問",
            })

        assert resp.status_code == 404

    def test_異常系_自己送信_400(self, mock_app):
        """call_chain に自身が含まれる場合 400"""
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_b")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
                "call_chain": ["agent_b"],
            })

        assert resp.status_code == 400
        assert "ループ" in resp.json()["detail"] or "loop" in resp.json()["detail"].lower()

    def test_異常系_タイムアウト_504(self, mock_app):
        """CLIBridge がタイムアウトした場合 504"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent()

        import asyncio

        async def slow_stream(*a, **kw):
            raise asyncio.TimeoutError()
            yield  # noqa: unreachable — AsyncGenerator にするため

        mock_bridge.run_stream = MagicMock(return_value=slow_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
            })

        assert resp.status_code == 504

    def test_正常系_複数チャンクの結合(self, mock_app):
        """複数の assistant イベントのテキストが結合される"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent()

        async def fake_stream(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "前半"}]},
            }
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "後半"}]},
            }
            yield {
                "type": "result",
                "session_id": "sess-multi",
                "result": "前半後半",
            }

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "前半" in data["response"]
        assert "後半" in data["response"]

    def test_正常系_pingイベントは無視される(self, mock_app):
        """_ping イベントはレスポンスに影響しない"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent()

        async def fake_stream(*a, **kw):
            yield {"type": "_ping"}
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "回答"}]},
            }
            yield {"type": "_ping"}
            yield {
                "type": "result",
                "session_id": "sess-ping",
                "result": "回答",
            }

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["response"] == "回答"
        assert data["session_id"] == "sess-ping"


class TestLoopPrevention:
    """call_chain によるループ防止・深さ制限"""

    def test_循環検出_A_B_A(self, mock_app):
        """A→B→A のループを検出して 400"""
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_a")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_a",
                "message": "質問",
                "call_chain": ["system", "agent_a"],
            })

        assert resp.status_code == 400

    def test_循環検出_長いチェーン内(self, mock_app):
        """A→B→C→D→B のようにチェーン途中のIDが再登場"""
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_b")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "質問",
                "call_chain": ["agent_a", "agent_b", "agent_c"],
            })

        assert resp.status_code == 400

    def test_最大チェーン長超過_400(self, mock_app):
        """call_chain が最大長(5)に達している場合 400"""
        app, mock_config, _, _ = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_f")

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_f",
                "message": "質問",
                "call_chain": ["a1", "a2", "a3", "a4", "a5"],
            })

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "深" in detail or "max" in detail.lower() or "長" in detail

    def test_チェーン長4は許可される(self, mock_app):
        """call_chain が4（最大長未満）なら正常に処理される"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_e")

        async def fake_stream(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "OK"}]},
            }
            yield {"type": "result", "session_id": "sess-ok", "result": "OK"}

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_e",
                "message": "質問",
                "call_chain": ["a1", "a2", "a3", "a4"],
            })

        assert resp.status_code == 200

    def test_call_chain省略時は制限なし(self, mock_app):
        """call_chain を省略した場合、ループチェックも深さチェックもスキップ"""
        app, mock_config, _, mock_bridge = mock_app
        mock_config.get_agent.return_value = _make_agent(agent_id="agent_x")

        async def fake_stream(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "OK"}]},
            }
            yield {"type": "result", "session_id": "sess-nochain", "result": "OK"}

        mock_bridge.run_stream = MagicMock(return_value=fake_stream())

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_x",
                "message": "質問",
            })

        assert resp.status_code == 200
