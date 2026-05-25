"""SQLite persistence layer for task records."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .models import (
    BiliDeliveryStatus,
    BiliEventRecord,
    BiliEventStatus,
    BiliUserEmailRecord,
    MailStatus,
    TaskRecord,
    TaskStatus,
    utc_now_text,
)


class TaskRepository:
    """Small repository wrapping SQLite statements used by the app."""

    _ACTIVE_TASK_STATUSES = (
        TaskStatus.PENDING.value,
        TaskStatus.RESOLVING_VIDEO.value,
        TaskStatus.FETCHING_SUBTITLE.value,
        TaskStatus.DOWNLOADING_AUDIO.value,
        TaskStatus.TRANSCRIBING.value,
        TaskStatus.SUMMARIZING.value,
        TaskStatus.WRITING_MARKDOWN.value,
        TaskStatus.SENDING_MAIL.value,
    )

    _INTERRUPTED_TASK_MESSAGE = "服务重启后任务线程已不存在；已释放运行中状态，请重新提交该视频。"

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
                    send_mail INTEGER NOT NULL DEFAULT 1,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_checkpoint TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(connection, "summary_task", "send_mail", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "summary_task", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "summary_task", "last_checkpoint", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bili_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id TEXT NOT NULL UNIQUE,
                    comment_id TEXT NOT NULL DEFAULT '',
                    sender_mid TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    bvid TEXT NOT NULL DEFAULT '',
                    task_id INTEGER,
                    recipient_email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
                    delivered_at TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(connection, "bili_event", "recipient_email", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "bili_event", "delivery_status", "TEXT NOT NULL DEFAULT 'PENDING'")
            self._ensure_column(connection, "bili_event", "delivered_at", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bili_user_email (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _ensure_column(self, connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
        """Add a column for older local databases without forcing a manual migration."""
        existing_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")

    def create_task(self, source_input: str, bvid: str, send_mail: bool = True) -> TaskRecord:
        """Insert a new task in pending state and return the stored row."""
        now = utc_now_text()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO summary_task (
                    source_input, bvid, status, mail_status, send_mail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_input,
                    bvid,
                    TaskStatus.PENDING.value,
                    MailStatus.PENDING.value,
                    1 if send_mail else 0,
                    now,
                    now,
                ),
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
        return self.find_reusable_task_by_bvid(bvid, source_input, include_success=False)

    def find_reusable_task_by_bvid(
        self,
        bvid: str,
        source_input: str | None = None,
        include_success: bool = False,
    ) -> TaskRecord | None:
        """Find a task suitable for deduplication.

        B 站 @ 触发会复用成功结果，避免重复消耗转写和总结额度。带 `p=` 的
        多 P 链接按完整 source_input 区分，不和同 BV 的其他分集混用。
        """
        target_statuses = (
            (*self._ACTIVE_TASK_STATUSES, TaskStatus.SUCCESS.value)
            if include_success
            else self._ACTIVE_TASK_STATUSES
        )
        placeholders = ",".join("?" for _ in target_statuses)
        source_text = (source_input or "").strip()
        has_page_index = "?p=" in source_text or "&p=" in source_text
        with closing(self._connect()) as connection:
            if source_text and has_page_index:
                row = connection.execute(
                    f"""
                    SELECT * FROM summary_task
                    WHERE bvid = ? AND source_input = ? AND status IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (bvid, source_text, *target_statuses),
                ).fetchone()
            else:
                row = connection.execute(
                    f"""
                    SELECT * FROM summary_task
                    WHERE bvid = ?
                      AND source_input NOT LIKE '%?p=%'
                      AND source_input NOT LIKE '%&p=%'
                      AND status IN ({placeholders})
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (bvid, *target_statuses),
                ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def fail_interrupted_tasks(self) -> list[TaskRecord]:
        """Release tasks left in running states by a previous process.

        The worker is an in-process thread. After uvicorn is stopped or restarted,
        those old threads cannot resume, so keeping the rows as active would make
        later submissions keep reusing a dead task.
        """
        placeholders = ",".join("?" for _ in self._ACTIVE_TASK_STATUSES)
        now = utc_now_text()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM summary_task
                WHERE status IN ({placeholders})
                ORDER BY id ASC
                """,
                self._ACTIVE_TASK_STATUSES,
            ).fetchall()
            if rows:
                task_ids = [int(row["id"]) for row in rows]
                id_placeholders = ",".join("?" for _ in task_ids)
                connection.execute(
                    f"""
                    UPDATE summary_task
                    SET status = ?,
                        mail_status = ?,
                        error_message = ?,
                        updated_at = ?
                    WHERE id IN ({id_placeholders})
                    """,
                    (
                        TaskStatus.FAILED.value,
                        MailStatus.FAILED.value,
                        self._INTERRUPTED_TASK_MESSAGE,
                        now,
                        *task_ids,
                    ),
                )
                connection.commit()
        return [self._row_to_record(row) for row in rows]

    def prepare_interrupted_tasks_for_retry(self, max_retry_count: int = 2) -> list[TaskRecord]:
        """Move stale running tasks back to PENDING so the new process can retry them.

        The app uses an in-process worker thread. If the process is restarted,
        SQLite may still contain rows in running states, but their worker thread
        no longer exists. We retry them a small number of times and then fail the
        row explicitly to avoid an endless restart loop.
        """
        placeholders = ",".join("?" for _ in self._ACTIVE_TASK_STATUSES)
        now = utc_now_text()
        retryable_ids: list[int] = []
        failed_ids: list[int] = []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM summary_task
                WHERE status IN ({placeholders})
                ORDER BY id ASC
                """,
                self._ACTIVE_TASK_STATUSES,
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                retry_count = int(row["retry_count"])
                if retry_count < max_retry_count:
                    retryable_ids.append(task_id)
                else:
                    failed_ids.append(task_id)

            if retryable_ids:
                id_placeholders = ",".join("?" for _ in retryable_ids)
                connection.execute(
                    f"""
                    UPDATE summary_task
                    SET status = ?,
                        mail_status = ?,
                        retry_count = retry_count + 1,
                        last_checkpoint = ?,
                        error_message = '',
                        updated_at = ?
                    WHERE id IN ({id_placeholders})
                    """,
                    (
                        TaskStatus.PENDING.value,
                        MailStatus.PENDING.value,
                        "startup_retry",
                        now,
                        *retryable_ids,
                    ),
                )

            if failed_ids:
                id_placeholders = ",".join("?" for _ in failed_ids)
                connection.execute(
                    f"""
                    UPDATE summary_task
                    SET status = ?,
                        mail_status = ?,
                        last_checkpoint = ?,
                        error_message = ?,
                        updated_at = ?
                    WHERE id IN ({id_placeholders})
                    """,
                    (
                        TaskStatus.FAILED.value,
                        MailStatus.FAILED.value,
                        "retry_exhausted",
                        "自动重试次数已用完，请检查错误后手动重新提交。",
                        now,
                        *failed_ids,
                    ),
                )
            connection.commit()

        return [self.get_task(task_id) for task_id in retryable_ids]

    def reset_task_for_retry(self, task_id: int) -> TaskRecord:
        """Reset one failed task to PENDING while keeping reusable artifacts."""
        task = self.get_task(task_id)
        if task.status is TaskStatus.SUCCESS:
            return task
        if task.status.value in self._ACTIVE_TASK_STATUSES:
            return task
        return self.update_task_fields(
            task_id,
            status=TaskStatus.PENDING.value,
            mail_status=MailStatus.PENDING.value,
            retry_count=task.retry_count + 1,
            last_checkpoint="manual_retry",
            error_message="",
        )

    def reopen_bili_events_for_task(self, task_id: int) -> list[BiliEventRecord]:
        """Reopen failed B 站 events when their linked task is retried."""
        now = utc_now_text()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM bili_event
                WHERE task_id = ?
                  AND delivery_status != ?
                ORDER BY id ASC
                """,
                (task_id, BiliDeliveryStatus.SENT.value),
            ).fetchall()
            event_ids = [int(row["id"]) for row in rows]
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                connection.execute(
                    f"""
                    UPDATE bili_event
                    SET status = ?,
                        delivery_status = ?,
                        error_message = '',
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (
                        BiliEventStatus.TASK_CREATED.value,
                        BiliDeliveryStatus.PENDING.value,
                        now,
                        *event_ids,
                    ),
                )
                connection.commit()
        return [self.get_bili_event_by_id(event_id) for event_id in event_ids]

    def create_bili_event_if_absent(
        self,
        notification_id: str,
        comment_id: str,
        sender_mid: str,
        sender_name: str,
        content: str,
    ) -> BiliEventRecord:
        """Insert a B 站通知 once and return the stored event."""
        now = utc_now_text()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO bili_event (
                    notification_id, comment_id, sender_mid, sender_name,
                    content, status, delivery_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    comment_id,
                    sender_mid,
                    sender_name,
                    content,
                    BiliEventStatus.NEW.value,
                    BiliDeliveryStatus.PENDING.value,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get_bili_event_by_notification_id(notification_id)

    def upsert_bili_user_email(self, uid: str, username: str, email: str) -> BiliUserEmailRecord:
        """Create or update one B 站用户到邮箱的绑定关系."""
        now = utc_now_text()
        uid_text = uid.strip()
        username_text = username.strip()
        email_text = email.strip()
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO bili_user_email (uid, username, email, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    username = excluded.username,
                    email = excluded.email,
                    updated_at = excluded.updated_at
                """,
                (uid_text, username_text, email_text, now, now),
            )
            connection.commit()
        record = self.find_bili_user_email_by_uid(uid_text)
        if record is None:
            raise KeyError(f"bili user email {uid_text} not found")
        return record

    def delete_bili_user_email(self, uid: str) -> None:
        """Delete one local B 站用户邮箱 binding by uid."""
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM bili_user_email WHERE uid = ?", (uid.strip(),))
            connection.commit()

    def find_bili_user_email_by_uid(self, uid: str) -> BiliUserEmailRecord | None:
        """Find one email binding by B 站 user uid."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM bili_user_email WHERE uid = ?",
                (uid.strip(),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_bili_user_email(row)

    def list_bili_user_emails(self, limit: int = 200) -> list[BiliUserEmailRecord]:
        """Return local B 站 user email bindings for the management page."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM bili_user_email ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_bili_user_email(row) for row in rows]

    def get_bili_event_by_notification_id(self, notification_id: str) -> BiliEventRecord:
        """Load a B 站 event by its upstream notification id."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM bili_event WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"bili event {notification_id} not found")
        return self._row_to_bili_event(row)

    def get_bili_event_by_id(self, event_id: int) -> BiliEventRecord:
        """Load one B 站 event by the local SQLite id."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM bili_event WHERE id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"bili event {event_id} not found")
        return self._row_to_bili_event(row)

    def list_bili_events(self, limit: int = 20) -> list[BiliEventRecord]:
        """Return recent B 站 events for the local page."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM bili_event ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_bili_event(row) for row in rows]

    def list_open_bili_events(self, limit: int = 100) -> list[BiliEventRecord]:
        """Return events whose linked task status still needs to be synchronized."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM bili_event
                WHERE status = ?
                  AND task_id IS NOT NULL
                  AND delivery_status = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (BiliEventStatus.TASK_CREATED.value, BiliDeliveryStatus.PENDING.value, limit),
            ).fetchall()
        return [self._row_to_bili_event(row) for row in rows]

    def update_bili_event_fields(self, event_id: int, **fields: object) -> BiliEventRecord:
        """Patch selected B 站 event columns and refresh updated_at."""
        if not fields:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM bili_event WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(f"bili event {event_id} not found")
            return self._row_to_bili_event(row)

        values: list[object] = []
        clauses: list[str] = []
        for key, value in fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = ?")
        values.append(utc_now_text())
        values.append(event_id)

        with closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE bili_event SET {', '.join(clauses)} WHERE id = ?",
                tuple(values),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM bili_event WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"bili event {event_id} not found")
        return self._row_to_bili_event(row)

    def update_task_fields(self, task_id: int, **fields: object) -> TaskRecord:
        """Patch selected columns and refresh the updated_at timestamp."""
        if not fields:
            return self.get_task(task_id)

        values: list[object] = []
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
            send_mail=bool(row["send_mail"]),
            retry_count=int(row["retry_count"]),
            last_checkpoint=str(row["last_checkpoint"]),
            error_message=str(row["error_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_bili_event(self, row: sqlite3.Row) -> BiliEventRecord:
        """Map one SQLite row to a typed B 站 event record."""
        task_id = row["task_id"]
        return BiliEventRecord(
            id=int(row["id"]),
            notification_id=str(row["notification_id"]),
            comment_id=str(row["comment_id"]),
            sender_mid=str(row["sender_mid"]),
            sender_name=str(row["sender_name"]),
            content=str(row["content"]),
            source_url=str(row["source_url"]),
            bvid=str(row["bvid"]),
            task_id=int(task_id) if task_id is not None else None,
            recipient_email=str(row["recipient_email"]),
            status=BiliEventStatus(str(row["status"])),
            delivery_status=BiliDeliveryStatus(str(row["delivery_status"])),
            delivered_at=str(row["delivered_at"]),
            error_message=str(row["error_message"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _row_to_bili_user_email(self, row: sqlite3.Row) -> BiliUserEmailRecord:
        """Map one SQLite row to a typed B 站用户邮箱 binding."""
        return BiliUserEmailRecord(
            id=int(row["id"]),
            uid=str(row["uid"]),
            username=str(row["username"]),
            email=str(row["email"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
