"""TaskManager単体テスト"""

from pathlib import Path

import pytest

from server.task_manager import TaskManager, _parse_frontmatter


# ================================================================
# ヘルパー
# ================================================================

TASK_MD = """\
---
title: テストタスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
---

## 背景

テスト用タスク。

## 作業ステップ

- [ ] ステップ1
- [x] ステップ2
"""

TASK_MD_SCHEDULE = """\
---
title: 定期タスク
agent: system
phase: draft
created: 2026-04-01T17:00:00Z
schedule: 毎週 月曜 10:00
---

定期実行のテスト。
"""


def _create_task(root: Path, task_id: str, content: str = TASK_MD) -> Path:
    """タスクMDファイルを作成するヘルパー"""
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    md_file = tasks_dir / f"{task_id}.md"
    md_file.write_text(content, encoding="utf-8")
    return md_file


# ================================================================
# _parse_frontmatter
# ================================================================

class TestParseFrontmatter:
    def test_正常なfrontmatterをパースできる(self):
        fm, body = _parse_frontmatter(TASK_MD)
        assert fm["title"] == "テストタスク"
        assert fm["agent"] == "system"
        assert fm["phase"] == "draft"
        assert "ステップ1" in body

    def test_frontmatterなしの場合(self):
        fm, body = _parse_frontmatter("本文だけ")
        assert fm == {}
        assert body == "本文だけ"

    def test_scheduleフィールドが取得できる(self):
        fm, _ = _parse_frontmatter(TASK_MD_SCHEDULE)
        assert fm["schedule"] == "毎週 月曜 10:00"


# ================================================================
# TaskManager初期化
# ================================================================

class TestTaskManagerInit:
    def test_ディレクトリが自動作成される(self, tmp_path):
        tm = TaskManager(tmp_path)
        assert (tmp_path / "tasks").is_dir()
        assert (tmp_path / ".kobito" / "tasks").is_dir()


# ================================================================
# list_tasks
# ================================================================

class TestListTasks:
    def test_タスクなしで空リスト(self, tmp_path):
        tm = TaskManager(tmp_path)
        assert tm.list_tasks() == []

    def test_タスクが一覧で返る(self, tmp_path):
        _create_task(tmp_path, "task_001")
        _create_task(tmp_path, "task_002")
        tm = TaskManager(tmp_path)
        tasks = tm.list_tasks()
        assert len(tasks) == 2
        ids = [t.task_id for t in tasks]
        assert "task_001" in ids
        assert "task_002" in ids

    def test_frontmatterが正しく読まれる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.list_tasks()[0]
        assert task.title == "テストタスク"
        assert task.agent == "system"
        # チェック済みチェックボックスがあるため _infer_phase により "doing"
        assert task.phase == "doing"
        assert task.approval == "pending"

    def test_メタデータファイルが自動生成される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.list_tasks()
        meta_file = tmp_path / ".kobito" / "tasks" / "task_001.json"
        assert meta_file.exists()

    def test_bodyが取得できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.list_tasks()[0]
        assert "ステップ1" in task.body

    def test_scheduleが読まれる(self, tmp_path):
        _create_task(tmp_path, "task_001", TASK_MD_SCHEDULE)
        tm = TaskManager(tmp_path)
        task = tm.list_tasks()[0]
        assert task.schedule == "毎週 月曜 10:00"

    def test_scheduleなしはNone(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.list_tasks()[0]
        assert task.schedule is None


# ================================================================
# get_task
# ================================================================

class TestGetTask:
    def test_タスクが取得できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.get_task("task_001")
        assert task.task_id == "task_001"
        assert task.title == "テストタスク"

    def test_存在しないタスクでFileNotFoundError(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.get_task("nonexistent")


# ================================================================
# approve
# ================================================================

class TestApprove:
    def test_承認するとapprovedになる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.approve("task_001")
        assert task.approval == "approved"
        assert task.approved_at is not None

    def test_承認すると実行順序に追加される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.approve("task_001")
        order = tm.get_order()
        assert "task_001" in order

    def test_二重承認でも順序に重複しない(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.approve("task_001")
        tm.approve("task_001")
        order = tm.get_order()
        assert order.count("task_001") == 1

    def test_存在しないタスクの承認でエラー(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.approve("nonexistent")


# ================================================================
# force_done
# ================================================================

class TestForceDone:
    def test_強制完了でphaseがdoneになる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.force_done("task_001")
        assert task.phase == "done"

    def test_MDファイルのphaseが書き換わる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.force_done("task_001")
        content = (tmp_path / "tasks" / "task_001.md").read_text(encoding="utf-8")
        assert "phase: done" in content

    def test_強制完了で実行順序から除去される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.approve("task_001")
        assert "task_001" in tm.get_order()
        tm.force_done("task_001")
        assert "task_001" not in tm.get_order()

    def test_存在しないタスクの強制完了でエラー(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.force_done("nonexistent")


# ================================================================
# delete
# ================================================================

class TestDelete:
    def test_MDファイルが削除される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.list_tasks()  # メタデータ自動生成
        tm.delete("task_001")
        assert not (tmp_path / "tasks" / "task_001.md").exists()

    def test_メタデータファイルが削除される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.list_tasks()  # メタデータ自動生成
        tm.delete("task_001")
        assert not (tmp_path / ".kobito" / "tasks" / "task_001.json").exists()

    def test_実行順序から除去される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.approve("task_001")
        tm.delete("task_001")
        assert "task_001" not in tm.get_order()

    def test_存在しないタスクの削除でもエラーにならない(self, tmp_path):
        tm = TaskManager(tmp_path)
        tm.delete("nonexistent")  # 例外が出なければOK


# ================================================================
# update_order / get_order
# ================================================================

class TestOrder:
    def test_順序を更新できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        _create_task(tmp_path, "task_002")
        tm = TaskManager(tmp_path)
        result = tm.update_order(["task_002", "task_001"])
        assert result == ["task_002", "task_001"]

    def test_存在しないタスクIDはフィルタされる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        result = tm.update_order(["task_001", "nonexistent"])
        assert result == ["task_001"]

    def test_空の順序(self, tmp_path):
        tm = TaskManager(tmp_path)
        assert tm.get_order() == []

    def test_get_orderで永続化された順序が返る(self, tmp_path):
        _create_task(tmp_path, "task_001")
        _create_task(tmp_path, "task_002")
        tm = TaskManager(tmp_path)
        tm.update_order(["task_002", "task_001"])
        # 新しいインスタンスで読み直し
        tm2 = TaskManager(tmp_path)
        assert tm2.get_order() == ["task_002", "task_001"]


# ================================================================
# add_session
# ================================================================

class TestAddSession:
    def test_作業セッションを追加できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.add_session("task_001", "sess-abc")
        assert "sess-abc" in task.sessions

    def test_同じセッションIDは重複しない(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.add_session("task_001", "sess-abc")
        task = tm.add_session("task_001", "sess-abc")
        assert task.sessions.count("sess-abc") == 1

    def test_複数セッションを追加できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.add_session("task_001", "sess-1")
        task = tm.add_session("task_001", "sess-2")
        assert len(task.sessions) == 2

    def test_存在しないタスクでエラー(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.add_session("nonexistent", "sess-abc")


# ================================================================
# set_talk_session
# ================================================================

class TestSetTalkSession:
    def test_相談セッションを設定できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.set_talk_session("task_001", "talk-001")
        assert task.talk_session_id == "talk-001"

    def test_上書きできる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.set_talk_session("task_001", "talk-001")
        task = tm.set_talk_session("task_001", "talk-002")
        assert task.talk_session_id == "talk-002"

    def test_存在しないタスクでエラー(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.set_talk_session("nonexistent", "talk-001")


# ================================================================
# update_body
# ================================================================

class TestUpdateBody:
    def test_本文を更新できる(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        task = tm.update_body("task_001", "## 新しい本文\n\n更新済み")
        assert "新しい本文" in task.body

    def test_frontmatterは維持される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.update_body("task_001", "新本文")
        task = tm.get_task("task_001")
        assert task.title == "テストタスク"
        assert task.phase == "draft"

    def test_存在しないタスクでエラー(self, tmp_path):
        tm = TaskManager(tmp_path)
        with pytest.raises(FileNotFoundError):
            tm.update_body("nonexistent", "本文")


# ================================================================
# メタデータ永続化
# ================================================================

class TestMetaPersistence:
    def test_承認状態が永続化される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.approve("task_001")
        # 新しいインスタンスで読み直し
        tm2 = TaskManager(tmp_path)
        task = tm2.get_task("task_001")
        assert task.approval == "approved"

    def test_セッション紐づけが永続化される(self, tmp_path):
        _create_task(tmp_path, "task_001")
        tm = TaskManager(tmp_path)
        tm.add_session("task_001", "sess-1")
        tm.set_talk_session("task_001", "talk-1")
        tm2 = TaskManager(tmp_path)
        task = tm2.get_task("task_001")
        assert "sess-1" in task.sessions
        assert task.talk_session_id == "talk-1"
