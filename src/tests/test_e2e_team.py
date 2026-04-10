"""E2Eテスト — チーム作成 → メッセージ送信 → ファシリテーターループ → メンバー回答表示

実際のLM StudioやCLIプロセスは起動せず、モックを使って
チームエージェントの一連のフロー（作成・セッション管理・チャット・履歴取得）を
統合的に検証する。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.config import AgentInfo, AgentNotFoundError


# --------------------------------------------------------------------------
# ヘルパー
# --------------------------------------------------------------------------

def _parse_sse(text: str) -> list[dict]:
    """SSEレスポンステキストを解析して event dict のリストを返す"""
    events = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _make_agent(agent_id: str, name: str, path: str = "/tmp", **kwargs) -> AgentInfo:
    return AgentInfo(id=agent_id, name=name, path=path, cli="claude", model_tier="quick", **kwargs)


def _make_team(team_id: str, name: str, members: list[str]) -> AgentInfo:
    return AgentInfo(id=team_id, name=name, type="team", members=members)


# --------------------------------------------------------------------------
# フィクスチャ
# --------------------------------------------------------------------------

@pytest.fixture
def team_app(tmp_path):
    """チーム + メンバー2名を含むテストアプリ"""
    from server.app import create_app

    member_a = _make_agent("member_a", "メンバーA", description="フロントエンド担当")
    member_b = _make_agent("member_b", "メンバーB", description="バックエンド担当")
    team = _make_team("team_001", "テストチーム", ["member_a", "member_b"])
    regular = _make_agent("regular_001", "通常エージェント")

    all_agents = {a.id: a for a in [member_a, member_b, team, regular]}

    mock_config = MagicMock()
    mock_config._data_dir = tmp_path
    mock_config.get_agent.side_effect = lambda aid: (
        all_agents[aid] if aid in all_agents
        else (_ for _ in ()).throw(AgentNotFoundError(aid))
    )
    mock_config.list_agents.return_value = list(all_agents.values())
    mock_config.get_setting.side_effect = lambda key, default=None: {
        "lmstudio_url": "http://localhost:1234/v1",
        "team_max_turns": 5,
    }.get(key, default)
    mock_config.add_team.return_value = team

    mock_reader = MagicMock()
    mock_bridge = MagicMock()
    mock_bridge.shutdown = AsyncMock()

    app = create_app(
        config_manager=mock_config,
        session_reader=mock_reader,
        cli_bridge=mock_bridge,
    )
    return app, mock_config, tmp_path


def _mock_ask_response(agent_id: str, agent_name: str, response: str) -> MagicMock:
    """httpx レスポンスモックを生成する"""
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "response": response,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "session_id": f"sess-{agent_id}",
    }
    return mock_resp


def _patch_httpx(return_value) -> MagicMock:
    """httpx.AsyncClient をモックするコンテキストを返す"""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=return_value)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_cls = MagicMock(return_value=mock_ctx)
    return mock_cls


# --------------------------------------------------------------------------
# チーム作成 API
# --------------------------------------------------------------------------

class TestTeamCreationAPI:
    """POST /api/agents/teams — チーム作成"""

    def test_チームを作成できる(self, team_app):
        app, mock_config, _ = team_app

        with TestClient(app) as client:
            resp = client.post("/api/agents/teams", json={
                "name": "新チーム",
                "description": "テスト用",
                "members": ["member_a", "member_b"],
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "テストチーム"   # mock_config.add_team が返す値
        assert data["type"] == "team"

        # add_team が正しい引数で呼ばれている
        mock_config.add_team.assert_called_once_with(
            name="新チーム",
            description="テスト用",
            members=["member_a", "member_b"],
        )

    def test_メンバーなしでエラー(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = client.post("/api/agents/teams", json={
                "name": "空チーム",
                "members": [],
            })

        assert resp.status_code == 400
        assert "メンバー" in resp.json()["detail"]


# --------------------------------------------------------------------------
# セッション一覧 API
# --------------------------------------------------------------------------

class TestTeamSessionsAPI:
    """GET /api/teams/{id}/sessions — セッション一覧"""

    def test_新規チームはセッションなし(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = client.get("/api/teams/team_001/sessions")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_存在しないチームは404(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = client.get("/api/teams/nonexistent/sessions")

        assert resp.status_code == 404


# --------------------------------------------------------------------------
# チームチャット API — SSEストリーム
# --------------------------------------------------------------------------

class TestTeamChatAPI:
    """POST /api/teams/{id}/chat — SSEストリーム"""

    def _do_chat(self, client, team_id: str, message: str, session_id: str | None = None):
        body = {"message": message}
        if session_id:
            body["session_id"] = session_id
        return client.post(f"/api/teams/{team_id}/chat", json=body)

    def test_SSEストリームが返る(self, team_app):
        app, _, _ = team_app

        mock_facilitator = MagicMock()
        mock_facilitator.call_facilitator.return_value = {"next": None}

        with patch("server.routes.teams.LMStudioClient", return_value=mock_facilitator):
            with TestClient(app) as client:
                resp = self._do_chat(client, "team_001", "議論を始めよう")

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_routing_chunk_done_イベントが順番に来る(self, team_app):
        """ファシリテーター → メンバーA回答 → 終了 の完全フロー"""
        app, _, _ = team_app

        # ファシリテーター: 1回目メンバーA、2回目終了
        mock_facilitator = MagicMock()
        mock_facilitator.call_facilitator.side_effect = [
            {"next": "member_a"},
            {"next": None},
        ]

        mock_http_cls = _patch_httpx(_mock_ask_response("member_a", "メンバーA", "私の意見はXです"))

        with patch("server.routes.teams.LMStudioClient", return_value=mock_facilitator):
            with patch("server.routes.teams.httpx.AsyncClient", mock_http_cls):
                with TestClient(app) as client:
                    resp = self._do_chat(client, "team_001", "プロジェクトの方針は？")

        assert resp.status_code == 200
        events = _parse_sse(resp.text)

        types = [e["type"] for e in events]
        assert "session_id" in types
        assert "routing" in types
        assert "chunk" in types
        assert "done" in types

        # イベント順: session_id → routing → chunk → done
        assert types.index("routing") < types.index("chunk")
        assert types.index("chunk") < types.index("done")

        # routing イベントにエージェント情報が含まれる
        routing_event = next(e for e in events if e["type"] == "routing")
        assert routing_event["agent_id"] == "member_a"
        assert routing_event["agent_name"] == "メンバーA"

        # chunk イベントに回答が含まれる
        chunk_event = next(e for e in events if e["type"] == "chunk")
        assert "私の意見はX" in chunk_event["data"]
        assert chunk_event["agent_id"] == "member_a"

    def test_複数メンバーが順番に回答する(self, team_app):
        """メンバーA → メンバーB → 終了の2ターンフロー"""
        app, _, _ = team_app

        mock_facilitator = MagicMock()
        mock_facilitator.call_facilitator.side_effect = [
            {"next": "member_a"},
            {"next": "member_b"},
            {"next": None},
        ]

        resp_a = _mock_ask_response("member_a", "メンバーA", "Aからの意見")
        resp_b = _mock_ask_response("member_b", "メンバーB", "Bからの意見")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[resp_a, resp_b])
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)

        with patch("server.routes.teams.LMStudioClient", return_value=mock_facilitator):
            with patch("server.routes.teams.httpx.AsyncClient", MagicMock(return_value=mock_ctx)):
                with TestClient(app) as client:
                    resp = self._do_chat(client, "team_001", "議論")

        events = _parse_sse(resp.text)
        chunk_events = [e for e in events if e["type"] == "chunk"]
        assert len(chunk_events) == 2
        assert chunk_events[0]["agent_id"] == "member_a"
        assert chunk_events[1]["agent_id"] == "member_b"

    def test_メッセージがセッションに保存される(self, team_app):
        """チャット後にセッションファイルが作成され履歴が記録される"""
        app, _, tmp_path = team_app

        mock_facilitator = MagicMock()
        mock_facilitator.call_facilitator.side_effect = [
            {"next": "member_a"},
            {"next": None},
        ]

        mock_http_cls = _patch_httpx(_mock_ask_response("member_a", "メンバーA", "Aの回答"))

        with patch("server.routes.teams.LMStudioClient", return_value=mock_facilitator):
            with patch("server.routes.teams.httpx.AsyncClient", mock_http_cls):
                with TestClient(app) as client:
                    resp = self._do_chat(client, "team_001", "質問です")

        assert resp.status_code == 200
        events = _parse_sse(resp.text)
        session_id_event = next(e for e in events if e["type"] == "session_id")
        session_id = session_id_event["data"]

        # セッションファイルが存在する
        session_file = tmp_path / "team_sessions" / "team_001" / f"{session_id}.json"
        assert session_file.exists(), f"セッションファイルが見つからない: {session_file}"

        session_data = json.loads(session_file.read_text(encoding="utf-8"))
        messages = session_data["messages"]

        # ユーザーメッセージが保存されている
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "質問です"

        # エージェントメッセージが保存されている
        agent_msgs = [m for m in messages if m["role"] == "agent"]
        assert len(agent_msgs) == 1
        assert agent_msgs[0]["agent_id"] == "member_a"
        assert "Aの回答" in agent_msgs[0]["content"]

    def test_通常エージェントへのチャットは400(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = self._do_chat(client, "regular_001", "こんにちは")

        assert resp.status_code == 400
        assert "チームエージェント" in resp.json()["detail"]

    def test_存在しないチームは404(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = self._do_chat(client, "nonexistent", "こんにちは")

        assert resp.status_code == 404


# --------------------------------------------------------------------------
# セッション履歴 API
# --------------------------------------------------------------------------

class TestTeamSessionDetailAPI:
    """GET /api/teams/{id}/sessions/{sid} — セッション詳細"""

    def test_セッション履歴が返る(self, team_app):
        app, _, tmp_path = team_app

        # セッションを手動で作成
        from server.team_session import TeamSessionManager
        mgr = TeamSessionManager(tmp_path)
        session = mgr.create_session("team_001", "テスト会話")
        mgr.append_message("team_001", session.session_id, {"role": "user", "content": "こんにちは"})
        mgr.append_message("team_001", session.session_id, {
            "role": "agent", "agent_id": "member_a", "agent_name": "メンバーA", "content": "こんにちは！"
        })

        with TestClient(app) as client:
            resp = client.get(f"/api/teams/team_001/sessions/{session.session_id}")

        assert resp.status_code == 200
        messages = resp.json()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "agent"
        assert messages[1]["agent_id"] == "member_a"
        assert messages[1]["agent_name"] == "メンバーA"

    def test_存在しないセッションは404(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = client.get("/api/teams/team_001/sessions/nonexistent")

        assert resp.status_code == 404


# --------------------------------------------------------------------------
# タイトル更新 API
# --------------------------------------------------------------------------

class TestTeamTitleUpdateAPI:
    """PUT /api/teams/{id}/sessions/{sid}/title"""

    def test_タイトルを更新できる(self, team_app):
        app, _, tmp_path = team_app

        from server.team_session import TeamSessionManager
        mgr = TeamSessionManager(tmp_path)
        session = mgr.create_session("team_001", "元タイトル")

        with TestClient(app) as client:
            resp = client.put(
                f"/api/teams/team_001/sessions/{session.session_id}/title",
                json={"title": "新タイトル"},
            )

        assert resp.status_code == 200
        assert resp.json()["title"] == "新タイトル"

        # ファイルに永続化されている
        updated = mgr.load_session("team_001", session.session_id)
        assert updated.title == "新タイトル"

    def test_存在しないセッションのタイトル更新は404(self, team_app):
        app, _, _ = team_app

        with TestClient(app) as client:
            resp = client.put(
                "/api/teams/team_001/sessions/nonexistent/title",
                json={"title": "新タイトル"},
            )

        assert resp.status_code == 404


# --------------------------------------------------------------------------
# E2Eフロー統合テスト
# --------------------------------------------------------------------------

class TestTeamE2EFlow:
    """チーム作成 → チャット → 履歴確認の統合フロー"""

    def test_チーム作成からチャット完了まで(self, team_app):
        """作成 → セッション確認(空) → チャット → セッション確認(1件) → 履歴確認"""
        app, _, tmp_path = team_app

        mock_facilitator = MagicMock()
        mock_facilitator.call_facilitator.side_effect = [
            {"next": "member_a"},
            {"next": None},
        ]

        mock_http_cls = _patch_httpx(
            _mock_ask_response("member_a", "メンバーA", "私が担当します")
        )

        with patch("server.routes.teams.LMStudioClient", return_value=mock_facilitator):
            with patch("server.routes.teams.httpx.AsyncClient", mock_http_cls):
                with TestClient(app) as client:
                    # 1. セッション一覧 → 空
                    resp = client.get("/api/teams/team_001/sessions")
                    assert resp.status_code == 200
                    assert resp.json() == []

                    # 2. チャット送信
                    resp = client.post("/api/teams/team_001/chat", json={
                        "message": "誰が担当しますか？",
                    })
                    assert resp.status_code == 200
                    events = _parse_sse(resp.text)
                    session_id = next(e["data"] for e in events if e["type"] == "session_id")

                    # done イベントが来ている
                    assert any(e["type"] == "done" for e in events)
                    # chunk イベントに回答が含まれる
                    chunk = next(e for e in events if e["type"] == "chunk")
                    assert "私が担当します" in chunk["data"]

                    # 3. セッション一覧 → 1件
                    resp = client.get("/api/teams/team_001/sessions")
                    assert resp.status_code == 200
                    sessions = resp.json()
                    assert len(sessions) == 1
                    assert sessions[0]["session_id"] == session_id

                    # 4. セッション履歴確認
                    resp = client.get(f"/api/teams/team_001/sessions/{session_id}")
                    assert resp.status_code == 200
                    messages = resp.json()

                    roles = [m["role"] for m in messages]
                    assert "user" in roles
                    assert "agent" in roles

                    agent_msg = next(m for m in messages if m["role"] == "agent")
                    assert agent_msg["agent_id"] == "member_a"
                    assert agent_msg["agent_name"] == "メンバーA"
                    assert "私が担当します" in agent_msg["content"]
