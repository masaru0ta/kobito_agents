"""TeamSessionManager のテスト"""

from __future__ import annotations

import json
import re

import pytest


TEAM_ID = "team_20260407_120000_abc"


# ---------------------------------------------------------------------------
# セッション作成
# ---------------------------------------------------------------------------

class TestCreateSession:
    """create_session: 新規セッションの生成"""

    def test_セッションを作成できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="PRレビュー")

        assert session.title == "PRレビュー"
        assert session.team_id == TEAM_ID
        assert session.messages == []

    def test_created_atが設定される(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")

        assert session.created_at != ""

    def test_session_idがユニーク形式(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        s1 = mgr.create_session(team_id=TEAM_ID, title="テスト1")
        s2 = mgr.create_session(team_id=TEAM_ID, title="テスト2")

        assert s1.session_id != s2.session_id

    def test_作成後にファイルが保存される(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="PRレビュー")

        path = tmp_data_dir / "team_sessions" / TEAM_ID / f"{session.session_id}.json"
        assert path.exists()


# ---------------------------------------------------------------------------
# 保存・読み込み
# ---------------------------------------------------------------------------

class TestSaveLoadSession:
    """save_session / load_session: ファイルI/O"""

    def test_セッションをファイルに保存できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")

        # ファイル内容を直接検証
        path = tmp_data_dir / "team_sessions" / TEAM_ID / f"{session.session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["session_id"] == session.session_id
        assert data["title"] == "テスト"
        assert data["team_id"] == TEAM_ID

    def test_セッションをファイルから読み込める(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        created = mgr.create_session(team_id=TEAM_ID, title="PRレビュー")

        loaded = mgr.load_session(team_id=TEAM_ID, session_id=created.session_id)

        assert loaded.session_id == created.session_id
        assert loaded.title == "PRレビュー"
        assert loaded.team_id == TEAM_ID

    def test_存在しないセッションの読み込みでエラー(self, tmp_data_dir):
        from server.team_session import TeamSessionManager, TeamSessionNotFoundError

        mgr = TeamSessionManager(tmp_data_dir)

        with pytest.raises(TeamSessionNotFoundError):
            mgr.load_session(team_id=TEAM_ID, session_id="nonexistent_id")

    def test_保存パスがteam_sessions_teamid_sessionid(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")

        expected = tmp_data_dir / "team_sessions" / TEAM_ID / f"{session.session_id}.json"
        assert expected.exists()


# ---------------------------------------------------------------------------
# セッション一覧
# ---------------------------------------------------------------------------

class TestListSessions:
    """list_sessions: チームのセッション一覧"""

    def test_セッション一覧を取得できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        mgr.create_session(team_id=TEAM_ID, title="セッション1")
        mgr.create_session(team_id=TEAM_ID, title="セッション2")

        sessions = mgr.list_sessions(team_id=TEAM_ID)
        assert len(sessions) == 2

    def test_セッションがない場合は空リスト(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        sessions = mgr.list_sessions(team_id=TEAM_ID)

        assert sessions == []

    def test_別チームのセッションは含まれない(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        other_team = "team_99999999_000000_xyz"
        mgr = TeamSessionManager(tmp_data_dir)
        mgr.create_session(team_id=TEAM_ID, title="チームAのセッション")
        mgr.create_session(team_id=other_team, title="チームBのセッション")

        sessions = mgr.list_sessions(team_id=TEAM_ID)
        assert len(sessions) == 1
        assert sessions[0].title == "チームAのセッション"


# ---------------------------------------------------------------------------
# タイトル編集
# ---------------------------------------------------------------------------

class TestUpdateTitle:
    """update_title: セッションタイトルの編集"""

    def test_タイトルを更新できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="旧タイトル")

        updated = mgr.update_title(team_id=TEAM_ID, session_id=session.session_id, title="新タイトル")

        assert updated.title == "新タイトル"

    def test_更新がファイルに永続化される(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="旧タイトル")
        mgr.update_title(team_id=TEAM_ID, session_id=session.session_id, title="新タイトル")

        reloaded = mgr.load_session(team_id=TEAM_ID, session_id=session.session_id)
        assert reloaded.title == "新タイトル"

    def test_存在しないセッションのタイトル更新でエラー(self, tmp_data_dir):
        from server.team_session import TeamSessionManager, TeamSessionNotFoundError

        mgr = TeamSessionManager(tmp_data_dir)

        with pytest.raises(TeamSessionNotFoundError):
            mgr.update_title(team_id=TEAM_ID, session_id="nonexistent", title="新タイトル")


# ---------------------------------------------------------------------------
# メッセージ追加
# ---------------------------------------------------------------------------

class TestAppendMessage:
    """append_message: セッションへのメッセージ追加"""

    def test_ユーザーメッセージを追加できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")

        updated = mgr.append_message(
            team_id=TEAM_ID,
            session_id=session.session_id,
            message={"role": "user", "content": "こんにちは"},
        )

        assert len(updated.messages) == 1
        assert updated.messages[0]["role"] == "user"
        assert updated.messages[0]["content"] == "こんにちは"

    def test_エージェントメッセージを追加できる(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")

        mgr.append_message(
            team_id=TEAM_ID,
            session_id=session.session_id,
            message={"role": "user", "content": "レビューお願いします"},
        )
        updated = mgr.append_message(
            team_id=TEAM_ID,
            session_id=session.session_id,
            message={
                "role": "agent",
                "agent_id": "agent_001",
                "agent_name": "レビュアーA",
                "content": "LGTMです",
            },
        )

        assert len(updated.messages) == 2
        assert updated.messages[1]["role"] == "agent"
        assert updated.messages[1]["agent_name"] == "レビュアーA"

    def test_追加がファイルに永続化される(self, tmp_data_dir):
        from server.team_session import TeamSessionManager

        mgr = TeamSessionManager(tmp_data_dir)
        session = mgr.create_session(team_id=TEAM_ID, title="テスト")
        mgr.append_message(
            team_id=TEAM_ID,
            session_id=session.session_id,
            message={"role": "user", "content": "テストメッセージ"},
        )

        reloaded = mgr.load_session(team_id=TEAM_ID, session_id=session.session_id)
        assert len(reloaded.messages) == 1
        assert reloaded.messages[0]["content"] == "テストメッセージ"
