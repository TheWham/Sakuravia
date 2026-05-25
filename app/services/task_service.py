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

    def submit_task(self, source_input: str) -> TaskRecord:
        """Create or reuse a task and make sure new work is scheduled exactly once."""
        bvid = self._bili_service.normalize_source(source_input)
        active = self._repository.find_active_task_by_bvid(bvid)
        if active is not None:
            return active

        task = self._repository.create_task(source_input=source_input.strip(), bvid=bvid)
        self._executor.submit(self._process_task, task.id)
        return task

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

            self._set_status(task_id, TaskStatus.RESOLVING_VIDEO)
            metadata = self._bili_service.fetch_metadata(task.bvid)
            self._repository.update_task_fields(task_id, video_title=metadata.title)

            self._set_status(task_id, TaskStatus.FETCHING_SUBTITLE)
            transcript = self._bili_service.fetch_subtitles(metadata)
            subtitle_source = "official_subtitle"
            audio_path: Path | None = None

            if transcript is None:
                subtitle_source = "asr_audio"
                self._set_status(task_id, TaskStatus.DOWNLOADING_AUDIO)
                audio_path = self._subtitle_audio_service.download_audio(metadata)
                self._repository.update_task_fields(task_id, audio_file_path=str(audio_path))

                self._set_status(task_id, TaskStatus.TRANSCRIBING)
                transcript = self._transcription_service.transcribe_audio(audio_path)

            self._repository.update_task_fields(task_id, subtitle_source=subtitle_source)

            self._set_status(task_id, TaskStatus.SUMMARIZING)
            summary = self._summary_service.summarize(metadata, transcript)

            self._set_status(task_id, TaskStatus.WRITING_MARKDOWN)
            markdown_path = self._artifact_service.write_markdown(metadata.bvid, metadata.title, summary.markdown)
            self._repository.update_task_fields(
                task_id,
                markdown_file_path=str(markdown_path),
                markdown_content=summary.markdown,
            )

            self._set_status(task_id, TaskStatus.SENDING_MAIL)
            self._mail_service.send_markdown(metadata.title, summary.markdown, markdown_path)
            self._repository.update_task_fields(task_id, mail_status=MailStatus.SENT.value)

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
