"""SessionReader (ClaudeSessionReader) のテスト"""

import json


from tests.conftest import make_session_jsonl


def _user_message(uuid, content, timestamp, session_id="sess-001", parent_uuid=None):
    """テスト用のuserメッセージ行を作る"""
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {"role": "user", "content": content},
        "timestamp": timestamp,
        "sessionId": session_id,
    }


def _assistant_message(uuid, content, timestamp, session_id="sess-001", parent_uuid=None, tool_uses=None):
    """テスト用のassistantメッセージ行を作る"""
    msg_content = [{"type": "text", "text": content}]
    if tool_uses:
        for tu in tool_uses:
            msg_content.append({"type": "tool_use", "name": tu["name"], "input": tu.get("input", {})})
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "message": {
            "role": "assistant",
            "content": msg_content,
            "model": "claude-opus-4-6",
        },
        "timestamp": timestamp,
        "sessionId": session_id,
    }


def _snapshot_message(uuid, timestamp):
    """テスト用のfile-history-snapshot行を作る"""
    return {
        "type": "file-history-snapshot",
        "messageId": uuid,
        "snapshot": {"messageId": uuid, "timestamp": timestamp},
    }


class TestProjectHash:
    """プロジェクトパスからproject_hashを算出"""

    def test_バックスラッシュとコロンがハイフンに変換される(self):
        from server.session_reader import ClaudeSessionReader

        reader = ClaudeSessionReader.__new__(ClaudeSessionReader)
        assert reader.get_project_hash("D:\\AI\\code\\kobito_agents") == "D--AI-code-kobito-agents"

    def test_スラッシュもハイフンに変換される(self):
        from server.session_reader import ClaudeSessionReader

        reader = ClaudeSessionReader.__new__(ClaudeSessionReader)
        assert reader.get_project_hash("D:/AI/code/kobito_agents") == "D--AI-code-kobito-agents"

    def test_ハイフンはそのまま(self):
        from server.session_reader import ClaudeSessionReader

        reader = ClaudeSessionReader.__new__(ClaudeSessionReader)
        assert reader.get_project_hash("D:\\AI\\code\\my-project") == "D--AI-code-my-project"


class TestListSessions:
    """セッション一覧の取得"""

    def test_セッション一覧が取得できる(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-001", [
            _user_message("u1", "こんにちは", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "やあ", "2026-04-01T06:00:05Z", parent_uuid="u1"),
        ])
        make_session_jsonl(claude_sessions_dir, "sess-002", [
            _user_message("u2", "仕様書を見て", "2026-04-01T07:00:00Z"),
            _assistant_message("a2", "確認した", "2026-04-01T07:00:10Z", parent_uuid="u2"),
            _user_message("u3", "ありがとう", "2026-04-01T07:01:00Z", parent_uuid="a2"),
            _assistant_message("a3", "どういたしまして", "2026-04-01T07:01:05Z", parent_uuid="u3"),
        ])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        sessions = reader.list_sessions(str(tmp_project_dir))

        assert len(sessions) == 2

    def test_セッション一覧が新しい順にソートされる(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-old", [
            _user_message("u1", "古い会話", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "応答", "2026-04-01T06:00:05Z"),
        ])
        make_session_jsonl(claude_sessions_dir, "sess-new", [
            _user_message("u2", "新しい会話", "2026-04-01T08:00:00Z"),
            _assistant_message("a2", "応答", "2026-04-01T08:00:05Z"),
        ])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        sessions = reader.list_sessions(str(tmp_project_dir))

        assert sessions[0].session_id == "sess-new"
        assert sessions[1].session_id == "sess-old"

    def test_メッセージ件数とプレビューが取得できる(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-001", [
            _user_message("u1", "最初のメッセージ", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "応答です", "2026-04-01T06:00:05Z"),
            _snapshot_message("s1", "2026-04-01T06:00:06Z"),  # カウントされない
            _user_message("u2", "2番目のメッセージ", "2026-04-01T06:01:00Z"),
            _assistant_message("a2", "最後の応答", "2026-04-01T06:01:05Z"),
        ])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        sessions = reader.list_sessions(str(tmp_project_dir))

        assert sessions[0].message_count == 4  # user/assistantのみ
        assert "最後の応答" in sessions[0].last_message

    def test_非表示フラグのセッションが除外される(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-visible", [
            _user_message("u1", "表示", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "OK", "2026-04-01T06:00:05Z"),
        ])
        make_session_jsonl(claude_sessions_dir, "sess-hidden", [
            _user_message("u2", "非表示", "2026-04-01T07:00:00Z"),
            _assistant_message("a2", "OK", "2026-04-01T07:00:05Z"),
        ])

        # 非表示メタデータを作成
        meta_dir = tmp_project_dir / ".kobito" / "meta"
        meta_dir.mkdir(parents=True)
        (meta_dir / "sess-hidden.json").write_text(
            json.dumps({"hidden": True}), encoding="utf-8"
        )

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        sessions = reader.list_sessions(str(tmp_project_dir))

        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-visible"


class TestReadSession:
    """セッション履歴の読み取り"""

    def test_user_assistantメッセージを抽出できる(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-001", [
            _snapshot_message("s0", "2026-04-01T05:59:00Z"),
            _user_message("u1", "こんにちは", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "やあ", "2026-04-01T06:00:05Z"),
        ])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        messages = reader.read_session(str(tmp_project_dir), "sess-001")

        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "こんにちは"
        assert messages[1].role == "assistant"
        assert messages[1].content == "やあ"

    def test_tool_use情報を抽出できる(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-001", [
            _user_message("u1", "ファイルを読んで", "2026-04-01T06:00:00Z"),
            _assistant_message("a1", "読んだ", "2026-04-01T06:00:05Z", tool_uses=[
                {"name": "Read", "input": {"file_path": "/tmp/test.py"}},
                {"name": "Bash", "input": {"command": "ls -la"}},
            ]),
        ])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        messages = reader.read_session(str(tmp_project_dir), "sess-001")

        assert len(messages) == 2
        assistant_msg = messages[1]
        assert len(assistant_msg.tool_uses) == 2
        assert assistant_msg.tool_uses[0]["name"] == "Read"
        assert assistant_msg.tool_uses[1]["name"] == "Bash"

    def test_セッションが空の場合(self, claude_sessions_dir, tmp_project_dir):
        from server.session_reader import ClaudeSessionReader

        make_session_jsonl(claude_sessions_dir, "sess-empty", [])

        reader = ClaudeSessionReader(claude_home=claude_sessions_dir.parent.parent)
        messages = reader.read_session(str(tmp_project_dir), "sess-empty")

        assert messages == []
