"""TaskManager — タスク管理"""

from __future__ import annotations

import json
import re
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class TaskMeta(BaseModel):
    task_id: str
    approval: str = "pending"  # pending / approved
    approved_at: Optional[str] = None
    sessions: list[str] = []
    talk_session_id: Optional[str] = None


class Task(BaseModel):
    task_id: str
    title: str
    agent: str
    phase: str  # draft / doing / done
    created: str
    schedule: Optional[str] = None
    approval: str
    approved_at: Optional[str] = None
    sessions: list[str] = []
    talk_session_id: Optional[str] = None
    body: str = ""


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """---で囲まれたfrontmatterをパースする"""
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)", content, re.DOTALL)
    if not m:
        return {}, content.strip()

    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()

    return fm, m.group(2).strip()


class TaskManager:
    def __init__(self, project_root: Path | str):
        self._root = Path(project_root)
        self._tasks_dir = self._root / "tasks"
        self._meta_dir = self._root / ".kobito" / "tasks"
        self._order_file = self._root / ".kobito" / "task_order.json"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # 内部ヘルパー
    # ----------------------------------------------------------------

    _VALID_APPROVALS = {"pending", "approved"}

    def _read_meta(self, task_id: str) -> TaskMeta:
        meta_file = self._meta_dir / f"{task_id}.json"
        if meta_file.exists():
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            meta = TaskMeta(**data)
            # 仕様外の approval 値を pending に正規化
            if meta.approval not in self._VALID_APPROVALS:
                meta.approval = "pending"
                self._write_meta(meta)
            return meta
        return TaskMeta(task_id=task_id)

    def _write_meta(self, meta: TaskMeta) -> None:
        meta_file = self._meta_dir / f"{meta.task_id}.json"
        meta_file.write_text(
            json.dumps(meta.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_order(self) -> list[str]:
        if self._order_file.exists():
            return json.loads(self._order_file.read_text(encoding="utf-8"))
        return []

    def _write_order(self, order: list[str]) -> None:
        self._order_file.parent.mkdir(parents=True, exist_ok=True)
        self._order_file.write_text(
            json.dumps(order, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_task(self, md_file: Path) -> Task | None:
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            return None
        fm, body = _parse_frontmatter(content)
        task_id = md_file.stem
        meta = self._read_meta(task_id)
        # メタデータが存在しない場合は自動生成
        if not (self._meta_dir / f"{task_id}.json").exists():
            self._write_meta(meta)
        return Task(
            task_id=task_id,
            title=fm.get("title", task_id),
            agent=fm.get("agent", "system"),
            phase=fm.get("phase", "draft"),
            created=fm.get("created", ""),
            schedule=fm.get("schedule") or None,
            approval=meta.approval,
            approved_at=meta.approved_at,
            sessions=meta.sessions,
            talk_session_id=meta.talk_session_id,
            body=body,
        )

    def _update_phase_in_md(self, task_id: str, phase: str) -> None:
        md_file = self._tasks_dir / f"{task_id}.md"
        if not md_file.exists():
            return
        content = md_file.read_text(encoding="utf-8")
        new_content = re.sub(
            r"^(phase:\s*).*$", f"phase: {phase}", content, flags=re.MULTILINE
        )
        md_file.write_text(new_content, encoding="utf-8")

    # ----------------------------------------------------------------
    # 公開API
    # ----------------------------------------------------------------

    def list_tasks(self) -> list[Task]:
        """tasks/ を走査してタスク一覧を返す"""
        tasks = []
        for md_file in sorted(self._tasks_dir.glob("*.md")):
            task = self._load_task(md_file)
            if task:
                tasks.append(task)
        return tasks

    def get_task(self, task_id: str) -> Task:
        md_file = self._tasks_dir / f"{task_id}.md"
        if not md_file.exists():
            raise FileNotFoundError(f"タスク '{task_id}' が見つかりません")
        task = self._load_task(md_file)
        if task is None:
            raise FileNotFoundError(f"タスク '{task_id}' を読み込めません")
        return task

    def approve(self, task_id: str) -> Task:
        self.get_task(task_id)  # 存在チェック
        meta = self._read_meta(task_id)
        meta.approval = "approved"
        meta.approved_at = datetime.now(timezone.utc).isoformat()
        self._write_meta(meta)
        order = self._read_order()
        if task_id not in order:
            order.append(task_id)
            self._write_order(order)
        return self.get_task(task_id)


    def force_done(self, task_id: str) -> Task:
        self.get_task(task_id)
        self._update_phase_in_md(task_id, "done")
        order = self._read_order()
        if task_id in order:
            order.remove(task_id)
            self._write_order(order)
        return self.get_task(task_id)

    def delete(self, task_id: str) -> None:
        md_file = self._tasks_dir / f"{task_id}.md"
        meta_file = self._meta_dir / f"{task_id}.json"
        if md_file.exists():
            md_file.unlink()
        if meta_file.exists():
            meta_file.unlink()
        order = self._read_order()
        if task_id in order:
            order.remove(task_id)
            self._write_order(order)

    def update_order(self, order: list[str]) -> list[str]:
        valid = {f.stem for f in self._tasks_dir.glob("*.md")}
        filtered = [tid for tid in order if tid in valid]
        self._write_order(filtered)
        return filtered

    def get_order(self) -> list[str]:
        return self._read_order()

    def add_session(self, task_id: str, session_id: str) -> Task:
        self.get_task(task_id)
        meta = self._read_meta(task_id)
        if session_id not in meta.sessions:
            meta.sessions.append(session_id)
            self._write_meta(meta)
        return self.get_task(task_id)

    def set_talk_session(self, task_id: str, session_id: str) -> Task:
        self.get_task(task_id)
        meta = self._read_meta(task_id)
        meta.talk_session_id = session_id
        self._write_meta(meta)
        return self.get_task(task_id)

    def update_body(self, task_id: str, body: str) -> Task:
        md_file = self._tasks_dir / f"{task_id}.md"
        if not md_file.exists():
            raise FileNotFoundError(f"タスク '{task_id}' が見つかりません")
        content = md_file.read_text(encoding="utf-8")
        m = re.match(r"^(---\r?\n.*?\r?\n---\r?\n?)", content, re.DOTALL)
        if m:
            new_content = m.group(1) + "\n" + body
        else:
            new_content = body
        md_file.write_text(new_content, encoding="utf-8")
        return self.get_task(task_id)
