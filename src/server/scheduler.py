"""Scheduler — タスク自動実行エンジン"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from server.cli_bridge import CLIBridge, parse_stream_event, resolve_model
from server.config import ConfigManager
from server.task_context import build_task_context
from server.task_manager import TaskManager
from server.routes.chat import _update_session_meta

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 600  # 10分
MAX_LOG_ENTRIES = 100

_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def should_reset(meta: "TaskMeta", now: datetime) -> bool:  # type: ignore[name-defined]
    """定期タスクのリセット条件を判定する。

    Returns:
        True = 今回のチェックでリセットすべき
    """
    if not meta.is_recurring:
        return False
    if meta.repeat_enabled is False:
        return False

    interval = meta.reset_interval

    if interval == "every_check":
        return True

    # last_reset_at をパース
    last = (
        datetime.fromisoformat(meta.last_reset_at)
        if meta.last_reset_at
        else None
    )

    if interval == "hourly":
        # reset_time = ":MM"
        reset_min = int(meta.reset_time.lstrip(":")) if meta.reset_time else 0
        if now.minute < reset_min:
            return False
        # 今時間すでにリセット済みか
        if last and last.year == now.year and last.month == now.month \
                and last.day == now.day and last.hour == now.hour:
            return False
        return True

    if interval == "daily":
        h, m = _parse_hhmm(meta.reset_time)
        reset_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < reset_today:
            return False
        if last and last.date() >= now.date():
            return False
        return True

    if interval == "weekly":
        target_wd = _WEEKDAY_MAP.get((meta.reset_weekday or "").lower(), -1)
        if now.weekday() != target_wd:
            return False
        h, m = _parse_hhmm(meta.reset_time)
        reset_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < reset_today:
            return False
        # 今週すでにリセット済みか（ISO週番号で比較）
        if last and last.isocalendar()[:2] >= now.isocalendar()[:2]:
            return False
        return True

    if interval == "monthly":
        if now.day != (meta.reset_monthday or -1):
            return False
        h, m = _parse_hhmm(meta.reset_time)
        reset_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < reset_today:
            return False
        # 今月すでにリセット済みか
        if last and (last.year, last.month) >= (now.year, now.month):
            return False
        return True

    return False


def _parse_hhmm(reset_time: str | None) -> tuple[int, int]:
    """'HH:MM' を (hour, minute) に変換する"""
    if not reset_time:
        return 0, 0
    parts = reset_time.split(":")
    return int(parts[0]), int(parts[1])


def reset_recurring_task(tm: "TaskManager", task_id: str, now: datetime) -> None:  # type: ignore[name-defined]
    """定期タスクをリセットする。

    - タスク本文の全チェックボックスを未完了に戻す
    - MDファイルの phase: done を除去（動的導出に委ねる）
    - last_reset_at を更新する
    """
    task = tm.get_task(task_id)

    # 全チェックボックスを未完了に戻す
    new_body = re.sub(r"- \[x\]", "- [ ]", task.body, flags=re.IGNORECASE)
    tm.update_body(task_id, new_body)

    # phase: done をMDから除去（frontmatterから削除して動的導出に委ねる）
    md_file = tm._tasks_dir / f"{task_id}.md"
    content = md_file.read_text(encoding="utf-8")
    content = re.sub(r"^phase:\s*done\s*\n", "", content, flags=re.MULTILINE)
    md_file.write_text(content, encoding="utf-8")

    # last_reset_at を更新
    meta = tm._read_meta(task_id)
    meta.last_reset_at = now.isoformat()
    tm._write_meta(meta)


def _count_checkboxes(body: str) -> tuple[int, int]:
    """(checked, total) を返す"""
    checked = len(re.findall(r"- \[x\]", body, re.IGNORECASE))
    total = checked + len(re.findall(r"- \[ \]", body))
    return checked, total


def _extract_checked_steps(body: str) -> set[str]:
    """チェック済みステップのテキスト集合を返す"""
    return {
        m.group(1).strip()
        for m in re.finditer(r"- \[x\]\s*(.+)", body, re.IGNORECASE)
    }


def _diff_completed_steps(body_before: str, body_after: str) -> list[str]:
    """新たに完了したステップのテキストリストを返す"""
    before = _extract_checked_steps(body_before)
    after = _extract_checked_steps(body_after)
    return sorted(after - before)


def _first_unchecked_step(body: str) -> str | None:
    """最初の未完了ステップのテキストを返す"""
    m = re.search(r"- \[ \]\s*(.+)", body)
    return m.group(1).strip() if m else None


class Scheduler:
    """タスク自動実行スケジューラー

    asyncioベースのタイマーループで、一定間隔ごとに
    task_order.json の先頭から実行可能なタスクを選定し、
    作業セッションを開始する。
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        cli_bridge: CLIBridge,
        interval: int = INTERVAL_SECONDS,
    ):
        self._config_manager = config_manager
        self._cli_bridge = cli_bridge
        self._interval = interval

        # 公開状態（前回の設定を復元）
        self.enabled: bool = config_manager.get_setting("scheduler_enabled", False)
        self.running: set[str] = set()  # 実行中エージェントIDのセット
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None

        # 内部
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._log_file = Path(config_manager._data_dir) / "scheduler_log.json"
        self._logs: list[dict] = self._load_logs()

    # ----------------------------------------------------------------
    # ライフサイクル
    # ----------------------------------------------------------------

    def start(self) -> None:
        """タイマーループを開始する（lifespan から呼ぶ）"""
        if self._loop_task is None or self._loop_task.done():
            if self.enabled:
                self.next_run = datetime.now(timezone.utc) + timedelta(seconds=self._interval)
                asyncio.create_task(self.tick())
            self._loop_task = asyncio.create_task(self.run_loop())
            logger.info("スケジューラー タイマーループ開始")

    async def stop(self) -> None:
        """タイマーループを停止する（lifespan 終了時）"""
        # stop_event で run_loop に停止を通知
        if self._stop_event:
            self._stop_event.set()
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            logger.info("スケジューラー タイマーループ停止")
        self._loop_task = None

    # ----------------------------------------------------------------
    # ON/OFF 制御
    # ----------------------------------------------------------------

    def toggle(self) -> dict:
        """ON/OFF を切り替え、切り替え後の状態を返す"""
        self.enabled = not self.enabled
        if self.enabled:
            self.next_run = datetime.now(timezone.utc) + timedelta(seconds=self._interval)
            # ONにした瞬間に即時実行
            asyncio.create_task(self.tick())
        else:
            self.next_run = None
        self._config_manager.set_setting("scheduler_enabled", self.enabled)
        logger.info(f"スケジューラー {'ON' if self.enabled else 'OFF'}")
        return self.status()

    def _load_logs(self) -> list[dict]:
        if self._log_file.exists():
            try:
                return json.loads(self._log_file.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _append_log(self, entry: dict) -> None:
        self._logs.append(entry)
        if len(self._logs) > MAX_LOG_ENTRIES:
            self._logs = self._logs[-MAX_LOG_ENTRIES:]
        self._log_file.write_text(
            json.dumps(self._logs, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_logs(self) -> list[dict]:
        return list(reversed(self._logs))  # 新しい順

    def status(self) -> dict:
        """現在の状態を辞書で返す"""
        return {
            "enabled": self.enabled,
            "running": bool(self.running),
            "running_agents": list(self.running),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
        }

    # ----------------------------------------------------------------
    # タイマーループ
    # ----------------------------------------------------------------

    async def run_loop(self) -> None:
        """interval 秒ごとに tick を実行するループ"""
        # 外部から asyncio.create_task(run_loop()) された場合も stop() で制御可能にする
        if self._loop_task is None:
            self._loop_task = asyncio.current_task()
        self._stop_event = asyncio.Event()
        try:
            while not self._stop_event.is_set():
                # stop_event が set されたら即座に抜ける
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._interval,
                    )
                    break  # stop_event が set された
                except asyncio.TimeoutError:
                    pass  # interval 経過 → tick へ
                await self.tick()
        except asyncio.CancelledError:
            raise

    async def tick(self) -> None:
        """1サイクルの実行判定"""
        if not self.enabled:
            return

        now = datetime.now(timezone.utc)
        self.last_run = now
        self.next_run = now + timedelta(seconds=self._interval)

        # 定期タスクのリセット判定
        self._process_recurring_resets(now)

        # アイドル状態の各エージェントのタスクを並行起動
        targets = self._select_tasks()
        if not targets:
            logger.info("スケジューラー: 実行対象タスクなし")
            return

        for task, agent_path, agent in targets:
            logger.info(f"スケジューラー: タスク '{task.task_id}' ({agent.name}) を実行開始")
            self.running.add(agent.id)
            asyncio.create_task(self._run_session(task.task_id, agent_path, agent, now))

    # ----------------------------------------------------------------
    # 定期リセット処理
    # ----------------------------------------------------------------

    def _process_recurring_resets(self, now: datetime) -> None:
        """全エージェントの定期タスクをスキャンし、リセット条件を満たすものをリセットする"""
        for agent in self._config_manager.list_agents():
            tm = TaskManager(agent.path)
            for md_file in sorted(tm._tasks_dir.glob("*.md")):
                task_id = md_file.stem
                meta = tm._read_meta(task_id)
                if not meta.is_recurring:
                    continue
                if should_reset(meta, now):
                    try:
                        reset_recurring_task(tm, task_id, now)
                        logger.info(
                            f"定期リセット: エージェント '{agent.name}' "
                            f"タスク '{task_id}' をリセットしました"
                        )
                    except Exception as e:
                        logger.error(
                            f"定期リセット: タスク '{task_id}' のリセット失敗: {e}",
                            exc_info=True,
                        )

    # ----------------------------------------------------------------
    # タスク選定
    # ----------------------------------------------------------------

    def _select_tasks(self) -> list[tuple]:
        """アイドル状態の各エージェントの次タスクを返す。

        Returns:
            [(Task, agent_path, AgentInfo), ...]
        """
        result = []
        for agent in self._config_manager.list_agents():
            if agent.id in self.running:
                continue  # 既に実行中のエージェントはスキップ
            tm = TaskManager(agent.path)
            for task_id in tm.get_order():
                try:
                    task = tm.get_task(task_id)
                except FileNotFoundError:
                    continue
                if task.approval != "approved":
                    continue
                if task.phase == "done":
                    continue
                result.append((task, agent.path, agent))
                break  # エージェントごとに1タスク
        return result

    # ----------------------------------------------------------------
    # セッション実行
    # ----------------------------------------------------------------

    async def _run_session(
        self,
        task_id: str,
        agent_path: str,
        agent,
        started_at: datetime,
    ) -> None:
        """作業セッションを実行し、完了時に running フラグを解除する"""
        log_entry: dict = {
            "timestamp": started_at.isoformat(),
            "task_id": task_id,
            "task_title": "",
            "agent_id": agent.id,
            "agent_name": agent.name,
            "session_id": None,
            "checked_before": 0,
            "total_before": 0,
            "checked_after": 0,
            "total_after": 0,
            "progress_changed": False,
            "completed_steps": [],
            "current_step": None,
            "error": None,
        }
        try:
            tm = TaskManager(agent_path)
            task = tm.get_task(task_id)
            log_entry["task_title"] = task.title

            # 実行前チェックボックス集計
            checked_before, total_before = _count_checkboxes(task.body)
            log_entry["checked_before"] = checked_before
            log_entry["total_before"] = total_before
            log_entry["current_step"] = _first_unchecked_step(task.body)

            # タスクコンテキスト注入
            context = build_task_context(task, "work")
            prompt = context + "\n\nタスク「" + task.title + "」について1ステップ作業してください。"

            model = resolve_model(agent.cli, agent.model_tier)

            # 既存の作業セッションがあれば最新を再利用、なければ新規作成
            session_id = task.sessions[-1] if task.sessions else None
            log_entry["session_id"] = session_id

            async for raw_event in self._cli_bridge.run_stream(
                project_path=agent_path,
                prompt=prompt,
                model=model,
                session_id=session_id,
            ):
                ev = parse_stream_event(raw_event)

                if ev.event_type == "result":
                    if ev.session_id:
                        log_entry["session_id"] = ev.session_id
                        _update_session_meta(agent_path, ev.session_id, {"cli": agent.cli})
                        if ev.session_id not in (task.sessions or []):
                            tm.add_session(task_id, ev.session_id)
                            logger.info(
                                f"スケジューラー: タスク '{task_id}' "
                                f"セッション '{ev.session_id}' 新規紐づけ"
                            )
                    break

            # 実行後チェックボックス集計・差分
            task_after = tm.get_task(task_id)
            checked_after, total_after = _count_checkboxes(task_after.body)
            completed = _diff_completed_steps(task.body, task_after.body)
            log_entry["checked_after"] = checked_after
            log_entry["total_after"] = total_after
            log_entry["progress_changed"] = bool(completed)
            log_entry["completed_steps"] = completed

        except Exception as e:
            log_entry["error"] = str(e)
            logger.error(f"スケジューラー: セッション実行エラー: {e}", exc_info=True)
        finally:
            self._append_log(log_entry)
            self.running.discard(agent.id)
            logger.info(f"スケジューラー: {agent.name} 実行中フラグ解除")
