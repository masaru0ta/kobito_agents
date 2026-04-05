"""MCPサーバー ask_agent のテスト"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAskAgentToolDefinition:
    """ask_agent ツールが正しく定義されているか"""

    def test_ツールが登録されている(self):
        from mcp_server.ask_agent import mcp

        tools = mcp._tool_manager.list_tools()
        names = [t.name for t in tools]
        assert "ask_agent" in names

    def test_必須パラメータが定義されている(self):
        from mcp_server.ask_agent import mcp

        tools = mcp._tool_manager.list_tools()
        tool = [t for t in tools if t.name == "ask_agent"][0]
        params = tool.parameters
        assert "agent_id" in params["properties"]
        assert "message" in params["properties"]
        assert "agent_id" in params.get("required", [])
        assert "message" in params.get("required", [])

    def test_session_idはオプション(self):
        from mcp_server.ask_agent import mcp

        tools = mcp._tool_manager.list_tools()
        tool = [t for t in tools if t.name == "ask_agent"][0]
        params = tool.parameters
        assert "session_id" in params["properties"]
        required = params.get("required", [])
        assert "session_id" not in required


class TestAskAgentRequest:
    """ask_agent が内部APIに正しいリクエストを送るか"""

    @pytest.mark.asyncio
    async def test_正常系_リクエスト構築(self):
        from mcp_server.ask_agent import mcp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "agent_id": "agent_b",
            "agent_name": "テストB",
            "session_id": "sess-001",
            "response": "回答です",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("mcp_server.ask_agent.httpx.AsyncClient", return_value=mock_client):
            result = await mcp.call_tool(
                "ask_agent",
                {"agent_id": "agent_b", "message": "質問です"},
            )

        # httpx.AsyncClient.post が正しい引数で呼ばれたか
        call_args = mock_client.post.call_args
        url = call_args[0][0]
        body = call_args[1].get("json") or call_args[0][1]

        assert "/api/internal/ask" in url
        assert body["agent_id"] == "agent_b"
        assert body["message"] == "質問です"

    @pytest.mark.asyncio
    async def test_session_id付きリクエスト(self):
        from mcp_server.ask_agent import mcp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "agent_id": "agent_b",
            "agent_name": "テストB",
            "session_id": "sess-existing",
            "response": "続き",
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("mcp_server.ask_agent.httpx.AsyncClient", return_value=mock_client):
            result = await mcp.call_tool(
                "ask_agent",
                {
                    "agent_id": "agent_b",
                    "message": "続きの質問",
                    "session_id": "sess-existing",
                },
            )

        call_args = mock_client.post.call_args
        body = call_args[1].get("json") or call_args[0][1]
        assert body["session_id"] == "sess-existing"
