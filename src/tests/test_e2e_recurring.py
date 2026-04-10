"""定期タスク設定 E2E テスト（Playwright / Phase10）"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest
import uvicorn

from server.app import create_app
from server.config import ConfigManager
from server.cli_bridge import CLIBridge

TASK_MD = """\
---
id: task_recurring_e2e
title: E2E定期タスクテスト
agent: coder
---

- [ ] ステップ1
- [ ] ステップ2
"""


@pytest.fixture(scope="module")
def test_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e_recurring")

    project_dir = base / "test_project"
    project_dir.mkdir()
    (project_dir / "CLAUDE.md").write_text("# テストプロジェクト", encoding="utf-8")

    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "task_recurring_e2e.md").write_text(TASK_MD, encoding="utf-8")

    data_dir = base / "data"
    data_dir.mkdir()
    agents = [{
        "id": "coder",
        "name": "coder",
        "path": str(project_dir),
        "description": "テスト用エージェント",
        "cli": "claude",
        "model_tier": "standard",
    }]
    (data_dir / "agents.json").write_text(json.dumps(agents, ensure_ascii=False), encoding="utf-8")

    config = ConfigManager(data_dir=data_dir, system_path=str(project_dir))
    reader = MagicMock()
    reader.list_sessions.return_value = []
    bridge = CLIBridge()
    app = create_app(config_manager=config, session_reader=reader, cli_bridge=bridge)

    port = 18500
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/agents").status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.1)

    yield {"url": f"http://127.0.0.1:{port}", "project_dir": project_dir}

    server.should_exit = True


def _open_task_context_menu(page, url: str):
    """エージェント選択 → タスクタブ → タスク詳細 → コンテキストメニューを開く"""
    page.goto(url)
    page.wait_for_selector(".agent-item")
    page.click(".agent-item")
    # タスクタブに切り替え
    page.wait_for_selector(".tab")
    tabs = page.query_selector_all(".tab")
    for tab in tabs:
        if "タスク" in tab.text_content():
            tab.click()
            break
    # タスクアイテムをクリックして詳細を開く
    page.wait_for_selector(".task-item")
    page.click(".task-item")
    # 詳細ヘッダーの ⋯ ボタンをクリック
    page.wait_for_selector(".task-more-btn")
    page.click(".task-more-btn")
    page.wait_for_selector("#task-context-menu.visible")


class TestRecurringMenuEntry:

    def test_コンテキストメニューに定期タスク設定が表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        menu = page.query_selector("#task-context-menu")
        assert menu is not None
        assert "定期タスク設定" in menu.text_content()


class TestRecurringSettingsPanel:

    def test_定期タスク設定をクリックするとパネルが開く(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        assert page.query_selector("#recurring-panel").is_visible()

    def test_パネルにトグルが表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        assert page.query_selector("#recurring-enabled-toggle") is not None

    def test_パネルにinterval選択が表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        assert page.query_selector("#recurring-interval-select") is not None

    def test_daily選択でreset_time入力欄が表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        page.select_option("#recurring-interval-select", "daily")
        assert page.query_selector("#recurring-time-input").is_visible()

    def test_weekly選択でreset_weekday選択欄が表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        page.select_option("#recurring-interval-select", "weekly")
        assert page.query_selector("#recurring-weekday-select").is_visible()

    def test_monthly選択でreset_monthday入力欄が表示される(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        page.select_option("#recurring-interval-select", "monthly")
        assert page.query_selector("#recurring-monthday-input").is_visible()


class TestRecurringSave:

    def test_daily設定を保存できる(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")

        # daily + 09:00 を設定して保存
        page.select_option("#recurring-interval-select", "daily")
        page.wait_for_selector("#recurring-time-input", state="visible")
        page.fill("#recurring-time-input", "09:00")
        page.click("#recurring-save-btn")

        # パネルが閉じる
        page.wait_for_selector("#recurring-panel", state="hidden")

    def test_保存後に再度開くと設定が残っている(self, test_env, page):
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")
        page.select_option("#recurring-interval-select", "daily")
        page.wait_for_selector("#recurring-time-input", state="visible")
        page.fill("#recurring-time-input", "10:00")
        page.click("#recurring-save-btn")
        page.wait_for_selector("#recurring-panel", state="hidden")

        # 再度開く
        _open_task_context_menu(page, test_env["url"])
        page.click("#ctx-recurring")
        page.wait_for_selector("#recurring-panel", state="visible")

        assert page.query_selector("#recurring-interval-select").input_value() == "daily"
        assert "10:00" in page.query_selector("#recurring-time-input").input_value()
