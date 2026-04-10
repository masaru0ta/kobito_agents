"""CLIBridgeのテスト"""


import pytest


class TestBuildCommand:
    """コマンド構築"""

    def test_stream_json入力モードが指定される(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus")

        assert "--input-format" in cmd
        assert "stream-json" in cmd
        assert "--output-format" in cmd

    def test_session_id指定時にresumeが付く(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus", session_id="sess-001")

        assert "--resume" in cmd
        assert "sess-001" in cmd

    def test_session_idなしの場合resumeが付かない(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus")

        assert "--resume" not in cmd

    def test_共通指示ファイルが追加される(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus")

        assert "--append-system-prompt-file" in cmd

    def test_mcp_configが含まれる(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus")

        assert "--mcp-config" in cmd
        # --mcp-config の次の引数がJSONファイルパスであること
        idx = cmd.index("--mcp-config")
        config_path = cmd[idx + 1]
        assert config_path.endswith(".json")

    def test_mcp_config_はresume時も含まれる(self):
        from server.cli_bridge import ClaudeAdapter

        adapter = ClaudeAdapter()
        cmd = adapter.build_command(model="opus", session_id="sess-001")

        assert "--mcp-config" in cmd


class TestModelMapping:
    """モデルティアからモデル名への変換"""

    def test_claude_deep_はopusになる(self):
        from server.cli_bridge import resolve_model

        assert resolve_model("claude", "deep") == "opus"

    def test_claude_quick_はsonnetになる(self):
        from server.cli_bridge import resolve_model

        assert resolve_model("claude", "quick") == "sonnet"

    def test_不明なティアでエラー(self):
        from server.cli_bridge import resolve_model

        with pytest.raises(ValueError):
            resolve_model("claude", "unknown_tier")


class TestStreamOutput:
    """ストリーミング出力のパース"""

    def test_stream_jsonからテキストチャンクを抽出できる(self):
        from server.cli_bridge import parse_stream_event

        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "こんにちは"}],
            },
        }
        parsed = parse_stream_event(event)

        assert parsed.text == "こんにちは"
        assert parsed.tool_uses == []

    def test_stream_jsonからtool_useを抽出できる(self):
        from server.cli_bridge import parse_stream_event

        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "ファイルを読む"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "test.py"}},
                ],
            },
        }
        parsed = parse_stream_event(event)

        assert parsed.text == "ファイルを読む"
        assert len(parsed.tool_uses) == 1
        assert parsed.tool_uses[0]["name"] == "Read"

    def test_resultイベントからsession_idを取得できる(self):
        from server.cli_bridge import parse_stream_event

        event = {
            "type": "result",
            "session_id": "sess-new-001",
            "result": "完了",
        }
        parsed = parse_stream_event(event)

        assert parsed.session_id == "sess-new-001"
        assert parsed.result_text == "完了"
