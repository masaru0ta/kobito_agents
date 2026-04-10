"""LMStudioClient のテスト"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest


BASE_URL = "http://localhost:1234/v1"

MEMBERS = [
    {"id": "agent_001", "name": "レビュアーA", "description": "フロントエンド専門"},
    {"id": "agent_002", "name": "レビュアーB", "description": "バックエンド専門"},
]

HISTORY = [
    {"role": "user", "content": "このPRをレビューしてください"},
    {"role": "agent", "agent_id": "agent_001", "agent_name": "レビュアーA", "content": "LGTMです"},
]


# ---------------------------------------------------------------------------
# 疎通確認
# ---------------------------------------------------------------------------

class TestIsRunning:
    """is_running: GET /v1/models で疎通確認"""

    def test_200応答ならTrueを返す(self):
        from server.lmstudio_client import LMStudioClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("server.lmstudio_client.requests.get", return_value=mock_resp):
            client = LMStudioClient(BASE_URL)
            assert client.is_running() is True

    def test_接続拒否ならFalseを返す(self):
        import requests as req
        from server.lmstudio_client import LMStudioClient

        with patch("server.lmstudio_client.requests.get", side_effect=req.ConnectionError()):
            client = LMStudioClient(BASE_URL)
            assert client.is_running() is False

    def test_タイムアウトならFalseを返す(self):
        import requests as req
        from server.lmstudio_client import LMStudioClient

        with patch("server.lmstudio_client.requests.get", side_effect=req.Timeout()):
            client = LMStudioClient(BASE_URL)
            assert client.is_running() is False

    def test_正しいエンドポイントに問い合わせる(self):
        from server.lmstudio_client import LMStudioClient

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("server.lmstudio_client.requests.get", return_value=mock_resp) as mock_get:
            client = LMStudioClient(BASE_URL)
            client.is_running()
            mock_get.assert_called_once()
            called_url = mock_get.call_args[0][0]
            assert called_url == f"{BASE_URL}/models"


# ---------------------------------------------------------------------------
# オンデマンド起動
# ---------------------------------------------------------------------------

class TestEnsureRunning:
    """ensure_running: 未起動なら lms server start を実行してポーリング"""

    def test_既に起動済みなら何もしない(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch.object(client, "is_running", return_value=True):
            with patch("server.lmstudio_client.subprocess.Popen") as mock_popen:
                client.ensure_running()
                mock_popen.assert_not_called()

    def test_未起動なら_lms_server_start_を実行する(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        # is_running: 最初False、その後True（起動後ポーリング成功）
        with patch.object(client, "is_running", side_effect=[False, True]):
            with patch("server.lmstudio_client.subprocess.Popen") as mock_popen:
                with patch("server.lmstudio_client.time.sleep"):
                    client.ensure_running()
                    mock_popen.assert_called_once()
                    cmd = mock_popen.call_args[0][0]
                    assert "lms" in cmd
                    assert "server" in cmd
                    assert "start" in cmd

    def test_ポーリングで起動を確認できればエラーなし(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        # False × 3 → True（3回ポーリング後に起動）
        side_effects = [False] + [False] * 3 + [True]
        with patch.object(client, "is_running", side_effect=side_effects):
            with patch("server.lmstudio_client.subprocess.Popen"):
                with patch("server.lmstudio_client.time.sleep"):
                    client.ensure_running()  # 例外なし

    def test_タイムアウトでLMStudioTimeoutErrorを送出する(self):
        from server.lmstudio_client import LMStudioClient, LMStudioTimeoutError

        client = LMStudioClient(BASE_URL)

        # 常に False（起動しない）
        with patch.object(client, "is_running", return_value=False):
            with patch("server.lmstudio_client.subprocess.Popen"):
                with patch("server.lmstudio_client.time.sleep"):
                    with pytest.raises(LMStudioTimeoutError):
                        client.ensure_running(timeout=3)


# ---------------------------------------------------------------------------
# ファシリテーター呼び出し
# ---------------------------------------------------------------------------

class TestCallFacilitator:
    """call_facilitator: LM Studio に次の発言者を決定させる"""

    def _make_response(self, next_agent):
        """LM Studio レスポンスのモックを作成"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"next": next_agent})
                    }
                }
            ]
        }
        return mock_resp

    def test_次の発言者IDを返す(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch("server.lmstudio_client.requests.post", return_value=self._make_response("agent_001")):
            result = client.call_facilitator(members=MEMBERS, title="PRレビュー", history=HISTORY)
            assert result == {"next": "agent_001"}

    def test_nullを返すと終了シグナル(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch("server.lmstudio_client.requests.post", return_value=self._make_response(None)):
            result = client.call_facilitator(members=MEMBERS, title="PRレビュー", history=HISTORY)
            assert result == {"next": None}

    def test_リクエストボディにメンバー情報が含まれる(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch("server.lmstudio_client.requests.post", return_value=self._make_response(None)) as mock_post:
            client.call_facilitator(members=MEMBERS, title="PRレビュー", history=HISTORY)

            payload = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            messages = payload["messages"]

            # システムプロンプトにメンバー名と説明が含まれる
            system_content = next(m["content"] for m in messages if m["role"] == "system")
            assert "レビュアーA" in system_content
            assert "レビュアーB" in system_content
            assert "フロントエンド専門" in system_content

    def test_リクエストボディに会議目的が含まれる(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch("server.lmstudio_client.requests.post", return_value=self._make_response(None)) as mock_post:
            client.call_facilitator(members=MEMBERS, title="PRレビュー会議", history=HISTORY)

            payload = mock_post.call_args[1].get("json") or mock_post.call_args[0][1]
            messages = payload["messages"]

            # ユーザーメッセージに会議目的が含まれる
            user_content = next(m["content"] for m in messages if m["role"] == "user")
            assert "PRレビュー会議" in user_content

    def test_正しいエンドポイントに送信する(self):
        from server.lmstudio_client import LMStudioClient

        client = LMStudioClient(BASE_URL)

        with patch("server.lmstudio_client.requests.post", return_value=self._make_response(None)) as mock_post:
            client.call_facilitator(members=MEMBERS, title="テスト", history=[])

            called_url = mock_post.call_args[0][0]
            assert called_url == f"{BASE_URL}/chat/completions"

    def test_レスポンスがJSONでなければエラー(self):
        from server.lmstudio_client import LMStudioClient, LMStudioResponseError

        client = LMStudioClient(BASE_URL)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "次はレビュアーAです"}}]  # JSONではない
        }

        with patch("server.lmstudio_client.requests.post", return_value=mock_resp):
            with pytest.raises(LMStudioResponseError):
                client.call_facilitator(members=MEMBERS, title="テスト", history=[])

    def test_HTTPエラー時にLMStudioResponseErrorを送出する(self):
        import requests as req
        from server.lmstudio_client import LMStudioClient, LMStudioResponseError

        client = LMStudioClient(BASE_URL)

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = req.HTTPError()

        with patch("server.lmstudio_client.requests.post", return_value=mock_resp):
            with pytest.raises(LMStudioResponseError):
                client.call_facilitator(members=MEMBERS, title="テスト", history=[])
