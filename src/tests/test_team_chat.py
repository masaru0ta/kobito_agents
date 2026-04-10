"""TeamChatProcessor のテスト"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MEMBERS = [
    {"id": "agent_001", "name": "レビュアーA", "description": "フロントエンド専門"},
    {"id": "agent_002", "name": "レビュアーB", "description": "バックエンド専門"},
]

HISTORY = [
    {"role": "user", "content": "このPRをレビューしてください"},
]


def _make_ask_result(agent_id, agent_name, content, session_id="sess_001"):
    """ask_agent の返却値モックを作成"""
    return {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "session_id": session_id,
        "response": content,
    }


# ---------------------------------------------------------------------------
# ファシリテーターループ
# ---------------------------------------------------------------------------

class TestFacilitatorLoop:
    """ファシリテーターの返却に応じた制御フロー"""

    @pytest.mark.asyncio
    async def test_nullで即終了しdoneイベントを発行(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.return_value = {"next": None}

        ask_fn = AsyncMock()

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        types = [e["type"] for e in events]
        assert "done" in types
        ask_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_idが返ったらask_agentを呼び出す(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        # 1回目: agent_001 → 2回目: null
        facilitator.call_facilitator.side_effect = [
            {"next": "agent_001"},
            {"next": None},
        ]

        ask_fn = AsyncMock(return_value=_make_ask_result("agent_001", "レビュアーA", "LGTMです"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        ask_fn.assert_called_once()
        call_kwargs = ask_fn.call_args
        assert call_kwargs[1]["agent_id"] == "agent_001" or call_kwargs[0][0] == "agent_001"

    @pytest.mark.asyncio
    async def test_max_turns到達で終了しdoneイベントを発行(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        # 常に agent_001 を返す（終わらない）
        facilitator.call_facilitator.return_value = {"next": "agent_001"}

        ask_fn = AsyncMock(return_value=_make_ask_result("agent_001", "レビュアーA", "回答"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=3,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        types = [e["type"] for e in events]
        assert "done" in types
        # ask_agent は最大3回呼ばれる
        assert ask_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_複数メンバーが順番に回答する(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.side_effect = [
            {"next": "agent_001"},
            {"next": "agent_002"},
            {"next": None},
        ]

        ask_fn = AsyncMock(side_effect=[
            _make_ask_result("agent_001", "レビュアーA", "フロントはOKです"),
            _make_ask_result("agent_002", "レビュアーB", "バックもOKです"),
        ])

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        assert ask_fn.call_count == 2


# ---------------------------------------------------------------------------
# SSE イベント
# ---------------------------------------------------------------------------

class TestSSEEvents:
    """発行される SSE イベントの種別・内容"""

    @pytest.mark.asyncio
    async def test_routing_イベントが発行される(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.side_effect = [
            {"next": "agent_001"},
            {"next": None},
        ]

        ask_fn = AsyncMock(return_value=_make_ask_result("agent_001", "レビュアーA", "回答"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        routing_events = [e for e in events if e["type"] == "routing"]
        assert len(routing_events) >= 1
        # routing イベントのデータに発言者名またはIDが含まれる
        assert "agent_001" in routing_events[0]["data"] or "レビュアーA" in routing_events[0]["data"]
        # agent_id / agent_name フィールドが含まれる
        assert routing_events[0]["agent_id"] == "agent_001"
        assert "agent_name" in routing_events[0]

    @pytest.mark.asyncio
    async def test_chunk_イベントが発行される(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.side_effect = [
            {"next": "agent_001"},
            {"next": None},
        ]

        ask_fn = AsyncMock(return_value=_make_ask_result("agent_001", "レビュアーA", "LGTMです"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        chunk_events = [e for e in events if e["type"] == "chunk"]
        assert len(chunk_events) >= 1
        assert "LGTMです" in chunk_events[-1]["data"]

    @pytest.mark.asyncio
    async def test_done_イベントが最後に発行される(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.return_value = {"next": None}

        ask_fn = AsyncMock()

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        assert events[-1]["type"] == "done"

    @pytest.mark.asyncio
    async def test_ask_agent_エラー時にerrorイベントを発行(self):
        from server.team_chat import TeamChatProcessor

        facilitator = MagicMock()
        facilitator.call_facilitator.return_value = {"next": "agent_001"}

        ask_fn = AsyncMock(side_effect=Exception("接続エラー"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        types = [e["type"] for e in events]
        assert "error" in types

    @pytest.mark.asyncio
    async def test_facilitator_エラー時にerrorイベントを発行(self):
        from server.team_chat import TeamChatProcessor
        from server.lmstudio_client import LMStudioResponseError

        facilitator = MagicMock()
        facilitator.call_facilitator.side_effect = LMStudioResponseError("パースエラー")

        ask_fn = AsyncMock()

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        events = []
        async for ev in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            events.append(ev)

        types = [e["type"] for e in events]
        assert "error" in types


# ---------------------------------------------------------------------------
# 履歴への追記
# ---------------------------------------------------------------------------

class TestHistoryAccumulation:
    """ファシリテーターに渡す履歴の蓄積"""

    @pytest.mark.asyncio
    async def test_各ターン後に履歴が蓄積される(self):
        from server.team_chat import TeamChatProcessor

        captured_histories = []

        def fake_facilitator(members, title, history):
            captured_histories.append(list(history))
            if len(captured_histories) == 1:
                return {"next": "agent_001"}
            return {"next": None}

        facilitator = MagicMock()
        facilitator.call_facilitator.side_effect = fake_facilitator

        ask_fn = AsyncMock(return_value=_make_ask_result("agent_001", "レビュアーA", "回答テキスト"))

        processor = TeamChatProcessor(
            lmstudio_client=facilitator,
            ask_agent_fn=ask_fn,
            max_turns=20,
        )

        async for _ in processor.process(
            members=MEMBERS, title="PRレビュー", history=HISTORY, user_message="お願いします"
        ):
            pass

        # 2回目のファシリテーター呼び出しにはエージェントの回答が含まれている
        assert len(captured_histories) == 2
        second_history = captured_histories[1]
        agent_messages = [m for m in second_history if m.get("role") == "agent"]
        assert len(agent_messages) >= 1
        assert "回答テキスト" in agent_messages[0]["content"]
