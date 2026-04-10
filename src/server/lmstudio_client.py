"""LM Studio との通信クライアント"""

from __future__ import annotations

import json
import subprocess
import time

import requests


class LMStudioTimeoutError(Exception):
    """LM Studio の起動がタイムアウトした"""


class LMStudioResponseError(Exception):
    """LM Studio からの応答が不正"""


class LMStudioClient:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # 疎通確認
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """GET /v1/models で LM Studio が起動しているか確認する"""
        try:
            resp = requests.get(f"{self._base_url}/models", timeout=3)
            return resp.status_code == 200
        except (requests.ConnectionError, requests.Timeout):
            return False

    # ------------------------------------------------------------------
    # オンデマンド起動
    # ------------------------------------------------------------------

    def ensure_running(self, timeout: int = 30) -> None:
        """未起動なら lms server start を実行し、起動するまでポーリングする"""
        if self.is_running():
            return

        subprocess.Popen(["lms", "server", "start"])

        for _ in range(timeout):
            time.sleep(1)
            if self.is_running():
                return

        raise LMStudioTimeoutError(
            f"LM Studio が {timeout} 秒以内に起動しませんでした"
        )

    # ------------------------------------------------------------------
    # ファシリテーター呼び出し
    # ------------------------------------------------------------------

    def call_facilitator(
        self,
        members: list[dict],
        title: str,
        history: list[dict],
    ) -> dict:
        """ファシリテーターに次の発言者を決定させる。

        Parameters
        ----------
        members:
            メンバー情報のリスト。各要素に `id`, `name`, `description` を含む。
        title:
            会議目的（セッションタイトル）。
        history:
            これまでの会話履歴。

        Returns
        -------
        dict
            ``{"next": "agent_id"}`` または ``{"next": None}``
        """
        system_prompt = self._build_system_prompt(members)
        user_message = self._build_user_message(title, history)

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        try:
            resp = requests.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                raise LMStudioResponseError(
                    "LM Studio にモデルがロードされていません。LM Studio でモデルをロードしてから再試行してください。"
                ) from e
            raise LMStudioResponseError(f"LM Studio HTTP エラー: {e}") from e
        except requests.ConnectionError:
            raise LMStudioResponseError(
                "LM Studio に接続できません。LM Studio が起動しているか確認してください。"
            )

        content = resp.json()["choices"][0]["message"]["content"]

        try:
            result = json.loads(content)
        except (json.JSONDecodeError, KeyError) as e:
            raise LMStudioResponseError(
                f"ファシリテーターの応答をJSONとして解析できません: {content!r}"
            ) from e

        if "next" not in result:
            raise LMStudioResponseError(
                f"ファシリテーターの応答に 'next' キーがありません: {result}"
            )

        return {"next": result["next"]}

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _build_system_prompt(self, members: list[dict]) -> str:
        member_lines = "\n".join(
            f"- {m['id']}: {m['name']}（{m.get('description', '')}）"
            for m in members
        )
        return (
            "あなたは会議のファシリテーターです。\n"
            "会議の流れを把握し、次に発言すべきメンバーを決定してください。\n"
            "議論が完結したと判断した場合は null を返してください。\n\n"
            "## メンバー一覧\n"
            f"{member_lines}\n\n"
            "## 出力形式\n"
            '次の発言者のIDを JSON で返してください: {"next": "agent_id"}\n'
            '議論を終了する場合: {"next": null}'
        )

    def _build_user_message(self, title: str, history: list[dict]) -> str:
        history_text = json.dumps(history, ensure_ascii=False, indent=2)
        return (
            f"会議目的: {title}\n\n"
            f"会話履歴:\n{history_text}\n\n"
            "次に発言すべきメンバーを選んでください。"
        )
