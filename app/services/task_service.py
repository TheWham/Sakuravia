"""End-to-end task orchestration and state transitions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..models import MailStatus, TaskRecord, TaskStatus
from ..storage import TaskRepository
from .artifact import ArtifactService
from .audio import SubtitleOrAudioService
from .bili import BiliResolverService
from .mail import MailService
from .summary import SummaryService
from .transcription import TranscriptionService


class TaskService:
    """Coordinate the whole pipeline while keeping each service focused."""

    def __init__(
        self,
        repository: TaskRepository,
        bili_service: BiliResolverService,
        subtitle_audio_service: SubtitleOrAudioService,
        transcription_service: TranscriptionService,
        summary_service: SummaryService,
        artifact_service: ArtifactService,
        mail_service: MailService,
    ) -> None:
        self._repository = repository
        self._bili_service = bili_service
        self._subtitle_audio_service = subtitle_audio_service
        self._transcription_service = transcription_service
        self._summary_service = summary_service
        self._artifact_service = artifact_service
        self._mail_service = mail_service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="summary-worker")

    def submit_task(self, source_input: str, reuse_success: bool = False, send_mail: bool = True) -> TaskRecord:
        """Create or reuse a task and make sure new work is scheduled exactly once.

        页面手动提交只复用运行中任务；B 站 @ 触发会额外复用历史成功任务，
        避免多人重复 @ 同一个视频时反复消耗外部 API 额度。B 站触发的新任务
        不发送默认邮箱，完成后由事件记录按请求用户绑定邮箱单独投递。
        """
        bvid = self._bili_service.normalize_source(source_input)
        active = self._repository.find_reusable_task_by_bvid(
            bvid,
            source_input.strip(),
            include_success=reuse_success,
        )
        if active is not None:
            return active

        task = self._repository.create_task(source_input=source_input.strip(), bvid=bvid, send_mail=send_mail)
        self._executor.submit(self._process_task, task.id)
        return task

    def retry_task(self, task_id: int) -> TaskRecord:
        """Reset one failed task and enqueue it again.

        The retry keeps existing artifacts such as downloaded audio or Markdown.
        `_process_task` will inspect those checkpoints and continue from the
        cheapest safe stage instead of blindly starting from scratch.
        """
        current = self._repository.get_task(task_id)
        if current.status is TaskStatus.SUCCESS:
            return current
        if self._is_active_status(current.status):
            return current
        task = self._repository.reset_task_for_retry(task_id)
        self._repository.reopen_bili_events_for_task(task.id)
        self._executor.submit(self._process_task, task.id)
        return self._repository.get_task(task.id)

    def recover_interrupted_tasks(self) -> list[TaskRecord]:
        """Schedule tasks left active by the previous process instance."""
        tasks = self._repository.prepare_interrupted_tasks_for_retry()
        for task in tasks:
            self._executor.submit(self._process_task, task.id)
        return tasks

    def list_tasks(self) -> list[TaskRecord]:
        """Return latest tasks for page polling and API access."""
        return self._repository.list_tasks()

    def get_task(self, task_id: int) -> TaskRecord:
        """Load one task for API detail responses."""
        return self._repository.get_task(task_id)

    def _process_task(self, task_id: int) -> None:
        """Run the configured pipeline and persist every visible state change."""
        try:
            task = self._repository.get_task(task_id)
            existing_markdown = self._load_existing_markdown(task)
            if existing_markdown is not None:
                markdown_content, markdown_path = existing_markdown
                title = task.video_title or task.bvid
                self._repository.update_task_fields(
                    task_id,
                    markdown_content=markdown_content,
                    markdown_file_path=str(markdown_path),
                    last_checkpoint="markdown_ready",
                )
                self._complete_mail_step(task_id, title, markdown_content, markdown_path)
                self._set_status(task_id, TaskStatus.SUCCESS)
                return

            self._set_status(task_id, TaskStatus.RESOLVING_VIDEO)
            metadata = self._bili_service.fetch_metadata_for_source(task.bvid, task.source_input)
            self._repository.update_task_fields(
                task_id,
                video_title=metadata.title,
                last_checkpoint="metadata",
            )

            self._set_status(task_id, TaskStatus.FETCHING_SUBTITLE)
            transcript = self._bili_service.fetch_subtitles(metadata)
            subtitle_source = "official_subtitle"
            audio_path: Path | None = None

            if transcript is None:
                subtitle_source = "asr_audio"
                audio_path = self._load_existing_audio(task)
                if audio_path is None:
                    self._set_status(task_id, TaskStatus.DOWNLOADING_AUDIO)
                    audio_path = self._subtitle_audio_service.download_audio(metadata)
                    self._repository.update_task_fields(
                        task_id,
                        audio_file_path=str(audio_path),
                        last_checkpoint="audio",
                    )

                self._set_status(task_id, TaskStatus.TRANSCRIBING)
                transcript = self._transcription_service.transcribe_audio(audio_path)

            self._repository.update_task_fields(task_id, subtitle_source=subtitle_source, last_checkpoint="transcript")

            self._set_status(task_id, TaskStatus.SUMMARIZING)
            summary = self._summary_service.summarize(metadata, transcript)
            self._repository.update_task_fields(task_id, markdown_content=summary.markdown, last_checkpoint="summary")

            self._set_status(task_id, TaskStatus.WRITING_MARKDOWN)
            markdown_path = self._artifact_service.write_markdown(metadata.bvid, metadata.title, summary.markdown)
            self._repository.update_task_fields(
                task_id,
                markdown_file_path=str(markdown_path),
                markdown_content=summary.markdown,
                last_checkpoint="markdown_ready",
            )

            self._complete_mail_step(task_id, metadata.title, summary.markdown, markdown_path)
            self._set_status(task_id, TaskStatus.SUCCESS)
        except Exception as exc:  # noqa: BLE001 - the page needs the original message.
            self._repository.update_task_fields(
                task_id,
                status=TaskStatus.FAILED.value,
                mail_status=MailStatus.FAILED.value,
                error_message=str(exc),
            )

    def _set_status(self, task_id: int, status: TaskStatus) -> None:
        """Centralize status updates so the pipeline stays readable."""
        self._repository.update_task_fields(task_id, status=status.value)

    def _is_active_status(self, status: TaskStatus) -> bool:
        """Return whether a task is already owned by the in-process worker."""
        return status in {
            TaskStatus.PENDING,
            TaskStatus.RESOLVING_VIDEO,
            TaskStatus.FETCHING_SUBTITLE,
            TaskStatus.DOWNLOADING_AUDIO,
            TaskStatus.TRANSCRIBING,
            TaskStatus.SUMMARIZING,
            TaskStatus.WRITING_MARKDOWN,
            TaskStatus.SENDING_MAIL,
        }

    def _load_existing_audio(self, task: TaskRecord) -> Path | None:
        """Return a completed audio artifact when retry can safely skip download."""
        if not task.audio_file_path:
            return None
        audio_path = Path(task.audio_file_path)
        if audio_path.exists() and audio_path.is_file() and audio_path.stat().st_size > 0:
            self._repository.update_task_fields(task.id, last_checkpoint="audio")
            return audio_path
        return None

    def _load_existing_markdown(self, task: TaskRecord) -> tuple[str, Path] | None:
        """Return a completed Markdown artifact so retry can jump to mail delivery."""
        if not task.markdown_file_path:
            return None
        markdown_path = Path(task.markdown_file_path)
        if not markdown_path.exists() or not markdown_path.is_file():
            return None
        markdown_content = task.markdown_content
        if not markdown_content:
            markdown_content = markdown_path.read_text(encoding="utf-8")
        if not markdown_content.strip():
            return None
        return markdown_content, markdown_path

    def _complete_mail_step(self, task_id: int, title: str, markdown: str, markdown_path: Path) -> None:
        """Send or skip default mail according to the task's stored delivery mode."""
        task = self._repository.get_task(task_id)
        if task.send_mail:
            if task.mail_status is not MailStatus.SENT:
                self._set_status(task_id, TaskStatus.SENDING_MAIL)
                self._mail_service.send_markdown(title, markdown, markdown_path)
                self._repository.update_task_fields(
                    task_id,
                    mail_status=MailStatus.SENT.value,
                    last_checkpoint="mail_sent",
                )
            return

        if task.mail_status is not MailStatus.SENT:
            self._repository.update_task_fields(
                task_id,
                mail_status=MailStatus.SKIPPED.value,
                last_checkpoint="mail_skipped",
            )
