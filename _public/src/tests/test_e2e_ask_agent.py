"""E2Eテスト — Agent A → Agent B → 回答の統合フロー

実際のCLIプロセスは起動せず、モックCLIBridgeを使って
内部API経由のリクエスト→ストリーム消費→レスポンス→メタ記録の
一連のフローを統合的に検証する。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo


def _agent(agent_id: str, name: str, path: str) -> AgentInfo:
    return AgentInfo(
        id=agent_id, name=name, path=path,
        cli="claude", model_tier="deep",
    )


@pytest.fixture
def e2e_app(tmp_path):
    """2エージェント構成のテストアプリ"""
    from server.app import create_app

    dir_a = tmp_path / "agent_a"
    dir_b = tmp_path / "agent_b"
    dir_a.mkdir()
    dir_b.mkdir()

    agents = {
        "agent_a": _agent("agent_a", "エージェントA", str(dir_a)),
        "agent_b": _agent("agent_b", "エージェントB", str(dir_b)),
    }

    mock_config = MagicMock()
    mock_config.get_agent.side_effect = lambda aid: agents.get(aid) or (_ for _ in ()).throw(
        __import__("server.config", fromlist=["AgentNotFoundError"]).AgentNotFoundError(aid)
    )

    mock_reader = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )
    return app, mock_config, mock_bridge, dir_a, dir_b


class TestE2EAskAgent:
    """Agent A → Agent B への質問・回答の統合フロー"""

    def test_A_がB_に質問して回答を得る(self, e2e_app):
        """基本的なE2Eフロー: 質問送信 → ストリーム消費 → JSON返却 → メタ記録"""
        app, _, mock_bridge, dir_a, dir_b = e2e_app

        async def fake_stream_b(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Bの回答です"}]},
            }
            yield {"type": "result", "session_id": "sess-b-001", "result": "Bの回答です"}

        mock_bridge.run_stream = MagicMock(return_value=fake_stream_b())

        with TestClient(app) as client:
            # Agent A が Agent B に質問（call_chain にAを含める）
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "このAPIの設計どう思う？",
                "call_chain": ["agent_a"],
            })

        # レスポンス検証
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent_b"
        assert data["agent_name"] == "エージェントB"
        assert data["session_id"] == "sess-b-001"
        assert "Bの回答" in data["response"]

        # メタデータ検証: initiated_by が記録されている
        meta_path = dir_b / ".kobito" / "meta" / "sess-b-001.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["initiated_by"] == "agent_a"

        # CLIBridge に正しい引数が渡されている
        call_args = mock_bridge.run_stream.call_args
        assert call_args[1]["project_path"] == str(dir_b) or call_args[0][0] == str(dir_b)

    def test_A_がB_に質問しB_がAに返信するとループ検出(self, e2e_app):
        """A→B→A の循環が call_chain で検出される"""
        app, _, _, _, _ = e2e_app

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "agent_a",
                "message": "Aに聞いてみよう",
                "call_chain": ["agent_a", "agent_b"],
            })

        assert resp.status_code == 400
        assert "ループ" in resp.json()["detail"]

    def test_セッション継続_2往復の会話(self, e2e_app):
        """1回目の会話 → session_id取得 → 2回目の会話で同じsession_idを使用"""
        app, _, mock_bridge, dir_a, dir_b = e2e_app

        # 1回目: 新規セッション
        async def stream_1(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "最初の回答"}]},
            }
            yield {"type": "result", "session_id": "sess-b-cont", "result": "最初の回答"}

        mock_bridge.run_stream = MagicMock(return_value=stream_1())

        with TestClient(app) as client:
            resp1 = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "最初の質問",
            })

        assert resp1.status_code == 200
        sid = resp1.json()["session_id"]
        assert sid == "sess-b-cont"

        # メタが書き込まれている
        meta_path = dir_b / ".kobito" / "meta" / "sess-b-cont.json"
        assert meta_path.exists()

        # 2回目: 同じセッション継続
        async def stream_2(*a, **kw):
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "続きの回答"}]},
            }
            yield {"type": "result", "session_id": "sess-b-cont", "result": "続きの回答"}

        mock_bridge.run_stream = MagicMock(return_value=stream_2())

        with TestClient(app) as client:
            resp2 = client.post("/api/internal/ask", json={
                "agent_id": "agent_b",
                "message": "続きの質問",
                "session_id": sid,
            })

        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["session_id"] == sid
        assert "続きの回答" in data2["response"]

        # 2回目はメタを上書きしていない（initiated_by が保持されている）
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["initiated_by"] == "system"

    def test_存在しないエージェントへの問い合わせ(self, e2e_app):
        """存在しない agent_id は 404"""
        app, _, _, _, _ = e2e_app

        with TestClient(app) as client:
            resp = client.post("/api/internal/ask", json={
                "agent_id": "nonexistent",
                "message": "質問",
            })

        assert resp.status_code == 404
