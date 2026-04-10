"""定期タスク機能テスト（Phase10）"""
import pytest
from datetime import datetime, timezone

from server.task_manager import TaskManager, TaskMeta
from server.scheduler import should_reset


# ----------------------------------------------------------------
# Step 1: TaskMeta フィールドテスト
# ----------------------------------------------------------------


class TestTaskMetaFields:
    """TaskMeta の定期タスクフィールドが存在・動作するか"""

    def test_reset_interval_default_none(self):
        meta = TaskMeta(task_id="t1")
        assert meta.reset_interval is None

    def test_reset_interval_field(self):
        meta = TaskMeta(task_id="t1", reset_interval="daily")
        assert meta.reset_interval == "daily"

    def test_repeat_enabled_default_none(self):
        meta = TaskMeta(task_id="t1")
        assert meta.repeat_enabled is None

    def test_repeat_enabled_false(self):
        meta = TaskMeta(task_id="t1", reset_interval="daily", repeat_enabled=False)
        assert meta.repeat_enabled is False

    def test_reset_time_field(self):
        meta = TaskMeta(task_id="t1", reset_interval="daily", reset_time="09:00")
        assert meta.reset_time == "09:00"

    def test_reset_weekday_field(self):
        meta = TaskMeta(
            task_id="t1",
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
        )
        assert meta.reset_weekday == "monday"

    def test_reset_monthday_field(self):
        meta = TaskMeta(
            task_id="t1",
            reset_interval="monthly",
            reset_monthday=1,
            reset_time="09:00",
        )
        assert meta.reset_monthday == 1

    def test_last_reset_at_field(self):
        meta = TaskMeta(
            task_id="t1",
            reset_interval="daily",
            last_reset_at="2026-04-10T09:00:00+00:00",
        )
        assert meta.last_reset_at == "2026-04-10T09:00:00+00:00"

    def test_is_recurring_true_when_interval_set(self):
        meta = TaskMeta(task_id="t1", reset_interval="daily")
        assert meta.is_recurring is True

    def test_is_recurring_false_when_no_interval(self):
        meta = TaskMeta(task_id="t1")
        assert meta.is_recurring is False


# ----------------------------------------------------------------
# Step 1: TaskManager 定期タスク設定メソッドテスト
# ----------------------------------------------------------------


@pytest.fixture
def tm(tmp_path):
    return TaskManager(tmp_path)


@pytest.fixture
def task_id(tmp_path):
    tid = "task_test_recurring"
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / f"{tid}.md").write_text(
        f'---\nid: {tid}\ntitle: "テスト"\n---\n\n- [ ] ステップ1\n- [ ] ステップ2\n',
        encoding="utf-8",
    )
    return tid


class TestTaskManagerRecurring:

    def test_set_recurring_daily(self, tm, task_id):
        tm.set_recurring(task_id, reset_interval="daily", reset_time="09:00")
        meta = tm._read_meta(task_id)
        assert meta.reset_interval == "daily"
        assert meta.reset_time == "09:00"
        assert meta.is_recurring is True

    def test_set_recurring_weekly(self, tm, task_id):
        tm.set_recurring(
            task_id,
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
        )
        meta = tm._read_meta(task_id)
        assert meta.reset_interval == "weekly"
        assert meta.reset_weekday == "monday"

    def test_set_recurring_monthly(self, tm, task_id):
        tm.set_recurring(
            task_id,
            reset_interval="monthly",
            reset_monthday=1,
            reset_time="09:00",
        )
        meta = tm._read_meta(task_id)
        assert meta.reset_interval == "monthly"
        assert meta.reset_monthday == 1

    def test_set_recurring_hourly(self, tm, task_id):
        tm.set_recurring(task_id, reset_interval="hourly", reset_time=":00")
        meta = tm._read_meta(task_id)
        assert meta.reset_interval == "hourly"
        assert meta.reset_time == ":00"

    def test_set_recurring_every_check(self, tm, task_id):
        tm.set_recurring(task_id, reset_interval="every_check")
        meta = tm._read_meta(task_id)
        assert meta.reset_interval == "every_check"

    def test_set_repeat_enabled_false(self, tm, task_id):
        tm.set_recurring(
            task_id, reset_interval="daily", reset_time="09:00", repeat_enabled=False
        )
        meta = tm._read_meta(task_id)
        assert meta.repeat_enabled is False

    def test_clear_recurring(self, tm, task_id):
        tm.set_recurring(task_id, reset_interval="daily", reset_time="09:00")
        tm.clear_recurring(task_id)
        meta = tm._read_meta(task_id)
        assert meta.reset_interval is None
        assert meta.is_recurring is False

    def test_get_recurring_when_set(self, tm, task_id):
        tm.set_recurring(task_id, reset_interval="daily", reset_time="09:00")
        result = tm.get_recurring(task_id)
        assert result["is_recurring"] is True
        assert result["reset_interval"] == "daily"
        assert result["reset_time"] == "09:00"

    def test_get_recurring_when_not_set(self, tm, task_id):
        result = tm.get_recurring(task_id)
        assert result["is_recurring"] is False
        assert result["reset_interval"] is None

    def test_meta_persisted_to_json(self, tm, task_id):
        """設定がJSONファイルとして永続化されるか"""
        tm.set_recurring(task_id, reset_interval="weekly", reset_weekday="friday", reset_time="18:00")
        # 別インスタンスで読み込み直しても同じ値が返る
        tm2 = TaskManager(tm._root)
        meta = tm2._read_meta(task_id)
        assert meta.reset_interval == "weekly"
        assert meta.reset_weekday == "friday"


# ----------------------------------------------------------------
# Step 3: 周期判定ロジックテスト
# ----------------------------------------------------------------

def _dt(iso: str) -> datetime:
    """ISO文字列をUTC datetimeに変換するヘルパー"""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestShouldReset:
    """should_reset(meta, now) の周期判定ロジックテスト"""

    # --- every_check ---

    def test_every_check_always_true(self):
        meta = TaskMeta(task_id="t1", reset_interval="every_check")
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is True

    def test_every_check_with_last_reset_still_true(self):
        """every_check は last_reset_at があっても常に True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="every_check",
            last_reset_at="2026-04-10T09:59:00+00:00",
        )
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is True

    # --- repeat_enabled: false ---

    def test_repeat_enabled_false_returns_false(self):
        """repeat_enabled=False ならどの interval でも False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="daily",
            reset_time="09:00",
            repeat_enabled=False,
        )
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is False

    def test_every_check_repeat_enabled_false_returns_false(self):
        meta = TaskMeta(
            task_id="t1",
            reset_interval="every_check",
            repeat_enabled=False,
        )
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is False

    # --- hourly ---

    def test_hourly_no_last_reset_returns_true(self):
        """last_reset_at なし → リセット対象"""
        meta = TaskMeta(task_id="t1", reset_interval="hourly", reset_time=":30")
        # 10:35 → 今時間の :30 を過ぎている
        now = _dt("2026-04-10T10:35:00")
        assert should_reset(meta, now) is True

    def test_hourly_not_reached_yet_returns_false(self):
        """指定分に達していない → False"""
        meta = TaskMeta(task_id="t1", reset_interval="hourly", reset_time=":30")
        # 10:25 → 今時間の :30 まだ
        now = _dt("2026-04-10T10:25:00")
        assert should_reset(meta, now) is False

    def test_hourly_already_reset_this_hour_returns_false(self):
        """今時間すでにリセット済み → False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="hourly",
            reset_time=":30",
            last_reset_at="2026-04-10T10:30:00+00:00",
        )
        now = _dt("2026-04-10T10:45:00")
        assert should_reset(meta, now) is False

    def test_hourly_next_hour_returns_true(self):
        """次の時間帯の :30 を過ぎた → True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="hourly",
            reset_time=":30",
            last_reset_at="2026-04-10T10:30:00+00:00",
        )
        now = _dt("2026-04-10T11:35:00")
        assert should_reset(meta, now) is True

    # --- daily ---

    def test_daily_no_last_reset_time_reached_returns_true(self):
        """last_reset_at なし、指定時刻以降 → True"""
        meta = TaskMeta(task_id="t1", reset_interval="daily", reset_time="09:00")
        now = _dt("2026-04-10T09:05:00")
        assert should_reset(meta, now) is True

    def test_daily_no_last_reset_before_time_returns_false(self):
        """last_reset_at なし、指定時刻前 → False"""
        meta = TaskMeta(task_id="t1", reset_interval="daily", reset_time="09:00")
        now = _dt("2026-04-10T08:55:00")
        assert should_reset(meta, now) is False

    def test_daily_already_reset_today_returns_false(self):
        """今日すでにリセット済み → False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="daily",
            reset_time="09:00",
            last_reset_at="2026-04-10T09:00:00+00:00",
        )
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is False

    def test_daily_yesterday_reset_time_passed_returns_true(self):
        """前日リセット、今日の指定時刻を過ぎた → True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="daily",
            reset_time="09:00",
            last_reset_at="2026-04-09T09:00:00+00:00",
        )
        now = _dt("2026-04-10T09:05:00")
        assert should_reset(meta, now) is True

    def test_daily_yesterday_reset_before_today_time_returns_false(self):
        """前日リセット、今日の指定時刻前 → False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="daily",
            reset_time="09:00",
            last_reset_at="2026-04-09T09:00:00+00:00",
        )
        now = _dt("2026-04-10T08:55:00")
        assert should_reset(meta, now) is False

    # --- weekly ---

    def test_weekly_correct_day_time_reached_no_last_reset(self):
        """該当曜日・指定時刻以降・last_reset_at なし → True"""
        # 2026-04-06 は月曜
        meta = TaskMeta(
            task_id="t1",
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
        )
        now = _dt("2026-04-06T09:05:00")
        assert should_reset(meta, now) is True

    def test_weekly_wrong_day_returns_false(self):
        """該当曜日でない → False"""
        # 2026-04-07 は火曜
        meta = TaskMeta(
            task_id="t1",
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
        )
        now = _dt("2026-04-07T10:00:00")
        assert should_reset(meta, now) is False

    def test_weekly_already_reset_this_week_returns_false(self):
        """今週すでにリセット済み → False"""
        # 2026-04-06 月曜に reset
        meta = TaskMeta(
            task_id="t1",
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
            last_reset_at="2026-04-06T09:00:00+00:00",
        )
        now = _dt("2026-04-06T10:00:00")
        assert should_reset(meta, now) is False

    def test_weekly_next_week_returns_true(self):
        """先週リセット済み、今週月曜の指定時刻を過ぎた → True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="weekly",
            reset_weekday="monday",
            reset_time="09:00",
            last_reset_at="2026-03-30T09:00:00+00:00",
        )
        now = _dt("2026-04-06T09:05:00")
        assert should_reset(meta, now) is True

    # --- monthly ---

    def test_monthly_correct_day_time_reached_no_last_reset(self):
        """該当日・指定時刻以降・last_reset_at なし → True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="monthly",
            reset_monthday=10,
            reset_time="09:00",
        )
        now = _dt("2026-04-10T09:05:00")
        assert should_reset(meta, now) is True

    def test_monthly_wrong_day_returns_false(self):
        """該当日でない → False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="monthly",
            reset_monthday=10,
            reset_time="09:00",
        )
        now = _dt("2026-04-11T10:00:00")
        assert should_reset(meta, now) is False

    def test_monthly_already_reset_this_month_returns_false(self):
        """今月すでにリセット済み → False"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="monthly",
            reset_monthday=10,
            reset_time="09:00",
            last_reset_at="2026-04-10T09:00:00+00:00",
        )
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is False

    def test_monthly_next_month_returns_true(self):
        """先月リセット済み、今月の指定日時を過ぎた → True"""
        meta = TaskMeta(
            task_id="t1",
            reset_interval="monthly",
            reset_monthday=10,
            reset_time="09:00",
            last_reset_at="2026-03-10T09:00:00+00:00",
        )
        now = _dt("2026-04-10T09:05:00")
        assert should_reset(meta, now) is True

    # --- reset_interval なし ---

    def test_non_recurring_returns_false(self):
        """reset_interval なし → 定期タスクでないので False"""
        meta = TaskMeta(task_id="t1")
        now = _dt("2026-04-10T10:00:00")
        assert should_reset(meta, now) is False


# ----------------------------------------------------------------
# Step 5: リセット実行テスト
# ----------------------------------------------------------------

from server.scheduler import reset_recurring_task  # noqa: E402


@pytest.fixture
def tm_reset(tmp_path):
    return TaskManager(tmp_path)


@pytest.fixture
def done_task_id(tmp_path, tm_reset):
    tid = "task_reset_test"
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks" / f"{tid}.md").write_text(
        f'---\nid: {tid}\ntitle: "定期テスト"\nphase: done\n---\n\n'
        f'- [x] ステップ1\n- [x] ステップ2\n- [x] ステップ3\n',
        encoding="utf-8",
    )
    return tid


NOW = _dt("2026-04-10T10:00:00")


class TestResetRecurringTask:

    def test_reset_unchecks_all_checkboxes(self, tm_reset, done_task_id):
        reset_recurring_task(tm_reset, done_task_id, NOW)
        task = tm_reset.get_task(done_task_id)
        assert "- [ ] ステップ1" in task.body
        assert "- [ ] ステップ2" in task.body
        assert "- [ ] ステップ3" in task.body
        assert "- [x]" not in task.body.lower()

    def test_reset_clears_done_phase(self, tm_reset, done_task_id):
        reset_recurring_task(tm_reset, done_task_id, NOW)
        task = tm_reset.get_task(done_task_id)
        assert task.phase != "done"

    def test_reset_updates_last_reset_at(self, tm_reset, done_task_id):
        reset_recurring_task(tm_reset, done_task_id, NOW)
        meta = tm_reset._read_meta(done_task_id)
        assert meta.last_reset_at == NOW.isoformat()
