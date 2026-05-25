"""SQLite persistence layer for task records."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .models import MailStatus, TaskRecord, TaskStatus, utc_now_text


class TaskRepository:
    """Small repository wrapping SQLite statements used by the app."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        # 当前 Windows 工作目录下，SQLite 默认的 DELETE journal 在提交时可能被文件锁策略拦住。
        # TRUNCATE 仍保留回滚日志能力，但提交时只清空 journal 文件，不依赖删除文件这个动作。
        connection.execute("PRAGMA journal_mode=TRUNCATE")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def init_db(self) -> None:
        """Create the task table used by the application."""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS summary_task (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_input TEXT NOT NULL,
                    bvid TEXT NOT NULL,
                    video_title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    subtitle_source TEXT NOT NULL DEFAULT '',
                    audio_file_path TEXT NOT NULL DEFAULT '',
                    markdown_file_path TEXT NOT NULL DEFAULT '',
                    markdown_content TEXT NOT NULL DEFAULT '',
                    mail_status TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def create_task(self, source_input: str, bvid: str) -> TaskRecord:
        """Insert a new task in pending state and return the stored row."""
        now = utc_now_text()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO summary_task (
                    source_input, bvid, status, mail_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_input, bvid, TaskStatus.PENDING.value, MailStatus.PENDING.value, now, now),
            )
            connection.commit()
            return self.get_task(cursor.lastrowid)

    def get_task(self, task_id: int) -> TaskRecord:
        """Load a single task or raise when the row does not exist."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM summary_task WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        return self._row_to_record(row)

    def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        """Return the latest tasks, newest first, for page polling."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM summary_task ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def find_active_task_by_bvid(self, bvid: str, source_input: str | None = None) -> TaskRecord | None:
        """Reuse unfinished work while allowing different selected parts to run separately."""
        active_statuses = (
            TaskStatus.PENDING.value,
            TaskStatus.RESOLVING_VIDEO.value,
            TaskStatus.FETCHING_SUBTITLE.value,
            TaskStatus.DOWNLOADING_AUDIO.value,
            TaskStatus.TRANSCRIBING.value,
            TaskStatus.SUMMARIZING.value,
            TaskStatus.WRITING_MARKDOWN.value,
            TaskStatus.SENDING_MAIL.value,
        )
        placeholders = ",".join("?" for _ in active_statuses)
        source_text = (source_input or "").strip()
        with closing(self._connect()) as connection:
            if source_text:
                row = connection.execute(
                    f"""
                    SELECT * FROM summary_task
                    WHERE bvid = ? AND source_input = ? AND status IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (bvid, source_text, *active_statuses),
                ).fetchone()
            else:
                row = connection.execute(
                    f"""
                    SELECT * FROM summary_task
                    WHERE bvid = ? AND status IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (bvid, *active_statuses),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def update_task_fields(self, task_id: int, **fields: str) -> TaskRecord:
        """Patch selected columns and refresh the updated_at timestamp."""
        if not fields:
            return self.get_task(task_id)

        values: list[str] = []
        clauses: list[str] = []
        for key, value in fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = ?")
        values.append(utc_now_text())
        values.append(str(task_id))

        with closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE summary_task SET {', '.join(clauses)} WHERE id = ?",
                tuple(values),
            )
            connection.commit()
        return self.get_task(task_id)

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        """Keep enum conversion in one place so the rest of the app stays clean."""
        return TaskRecord(
            id=int(row["id"]),
            source_input=str(row["source_input"]),
            bvid=str(row["bvid"]),
            video_title=str(row["video_title"]),
            status=TaskStatus(str(row["status"])),
            subtitle_source=str(row["subtitle_source"]),
            audio_file_path=str(row["audio_file_path"]),
            markdown_file_path=str(row["markdown_file_path"]),
            markdown_content=str(row["markdown_content"]),
            mail_status=MailStatus(str(row["mail_status"])),
            error_message=str(row["error_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
