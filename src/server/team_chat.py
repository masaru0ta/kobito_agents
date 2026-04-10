"""チームメッセージ処理 — ファシリテーターループと SSE イベント生成"""

from __future__ import annotations

import json
from typing import AsyncIterator, Callable


class TeamChatProcessor:
    """ファシリテーターループを実行し、SSE イベントを yield する。

    Parameters
    ----------
    lmstudio_client:
        ``call_facilitator(members, title, history)`` を持つオブジェクト。
    ask_agent_fn:
        ``async (agent_id, message, session_id) -> dict`` の非同期呼び出し可能オブジェクト。
        返却値は ``{"agent_id", "agent_name", "session_id", "response"}`` を含む dict。
    max_turns:
        ループの上限ターン数。
    """

    def __init__(
        self,
        lmstudio_client,
        ask_agent_fn: Callable,
        max_turns: int = 20,
    ):
        self._facilitator = lmstudio_client
        self._ask_agent_fn = ask_agent_fn
        self._max_turns = max_turns

    async def process(
        self,
        members: list[dict],
        title: str,
        history: list[dict],
        user_message: str,
    ) -> AsyncIterator[dict]:
        """ファシリテーターループを実行して SSE イベントを yield する。

        Yields
        ------
        dict
            ``{"type": "routing", "data": str}``  — 発言者決定時
            ``{"type": "chunk", "data": str, "agent_id": str, "agent_name": str}``  — 回答取得時
            ``{"type": "done"}``  — ループ終了時
            ``{"type": "error", "data": str}``  — エラー発生時
        """
        working_history = list(history) + [{"role": "user", "content": user_message}]

        try:
            for _ in range(self._max_turns):
                result = self._facilitator.call_facilitator(
                    members=members,
                    title=title,
                    history=working_history,
                )
                next_agent_id = result["next"]

                if next_agent_id is None:
                    yield {"type": "done"}
                    return

                # メンバー情報を取得
                member = next((m for m in members if m["id"] == next_agent_id), None)
                member_name = member["name"] if member else next_agent_id

                yield {
                    "type": "routing",
                    "data": f"{next_agent_id} ({member_name}) が回答中...",
                    "agent_id": next_agent_id,
                    "agent_name": member_name,
                }

                # メンバーに問い合わせ
                member_message = self._build_member_message(title, working_history)
                ask_result = await self._ask_agent_fn(
                    agent_id=next_agent_id,
                    message=member_message,
                    session_id=None,
                )

                response_text = ask_result["response"]
                agent_name = ask_result.get("agent_name", member_name)

                yield {
                    "type": "chunk",
                    "data": response_text,
                    "agent_id": next_agent_id,
                    "agent_name": agent_name,
                }

                working_history.append({
                    "role": "agent",
                    "agent_id": next_agent_id,
                    "agent_name": agent_name,
                    "content": response_text,
                })

            # max_turns 到達
            yield {"type": "done"}

        except Exception as e:
            yield {"type": "error", "data": str(e)}

    def _build_member_message(self, title: str, history: list[dict]) -> str:
        history_text = json.dumps(history, ensure_ascii=False, indent=2)
        return f"会議「{title}」の会話履歴:\n{history_text}"
