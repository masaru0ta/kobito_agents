"""タスクコンテキスト注入 — セッション開始時にタスク情報をプロンプトに付加する"""

from __future__ import annotations

from pathlib import Path

from server.task_manager import Task

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "assets" / "prompts"


def build_task_context(task: Task, mode: str) -> str:
    """テンプレートにタスク情報を展開してコンテキストブロックを返す"""
    template_file = TEMPLATES_DIR / f"task_{mode}.md"
    template = template_file.read_text(encoding="utf-8")
    return template.format(
        title=task.title,
        phase=task.phase,
        approval=task.approval,
        task_body=task.body,
    )
