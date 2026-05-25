"""FastAPI entrypoint for the local Bilibili AI assistant."""

from __future__ import annotations

from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import AppConfig
from .models import TaskStatus
from .storage import TaskRepository
from .utils import ValidationError
from .services.artifact import ArtifactService
from .services.audio import SubtitleOrAudioService
from .services.bili import BiliResolverService
from .services.bili_listener import BiliAuthService, BiliEventService, BiliHttpClient, BiliMentionApi, BiliMentionPoller
from .services.http_client import SimpleHttpClient
from .services.mail import MailService
from .services.process_runner import ProcessRunner
from .services.summary import SummaryService
from .services.task_service import TaskService
from .services.transcription import TranscriptionService


class TaskCreateRequest(BaseModel):
    """Incoming JSON payload for task creation."""

    source: str


class VideoOptionsRequest(BaseModel):
    """Incoming JSON payload for checking selectable video parts."""

    source: str


class BiliUserEmailRequest(BaseModel):
    """Incoming JSON payload for a local B 站用户邮箱 binding."""

    uid: str
    username: str
    email: str


def build_app() -> FastAPI:
    """Create the application and wire all services once at import time."""
    config = AppConfig.load()
    config.ensure_directories()

    repository = TaskRepository(config.sqlite_path)
    repository.init_db()

    process_runner = ProcessRunner()
    http_client = SimpleHttpClient()
    bili_service = BiliResolverService(config, process_runner)
    subtitle_audio_service = SubtitleOrAudioService(config, process_runner)
    transcription_service = TranscriptionService(config, http_client, subtitle_audio_service)
    summary_service = SummaryService(config, http_client)
    artifact_service = ArtifactService(config)
    mail_service = MailService(config)
    task_service = TaskService(
        repository=repository,
        bili_service=bili_service,
        subtitle_audio_service=subtitle_audio_service,
        transcription_service=transcription_service,
        summary_service=summary_service,
        artifact_service=artifact_service,
        mail_service=mail_service,
    )
    task_service.recover_interrupted_tasks()
    bili_http_client = BiliHttpClient(config)
    bili_auth_service = BiliAuthService(bili_http_client)
    bili_mention_api = BiliMentionApi(bili_http_client, config.bili_self_mid)
    bili_event_service = BiliEventService(repository, task_service, mail_service)
    bili_poller = BiliMentionPoller(config, bili_auth_service, bili_mention_api, bili_event_service)

    app = FastAPI(title="个人版 B 站 AI 助手", version="1.0.0")
    app.state.task_service = task_service
    app.state.bili_poller = bili_poller
    app.state.bili_event_service = bili_event_service
    app.state.config = config

    @app.on_event("startup")
    def start_bili_listener() -> None:
        """Start the optional B 站 listener after FastAPI finishes booting."""
        bili_poller.start()

    @app.on_event("shutdown")
    def stop_bili_listener() -> None:
        """Stop the B 站 listener so the process exits cleanly."""
        bili_poller.stop()

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        """Render the single-page local UI."""
        tasks = [task.to_dict() for task in task_service.list_tasks()]
        selected = tasks[0] if tasks else None
        listener_state = bili_poller.snapshot().to_dict()
        bili_events = [event.to_dict() for event in repository.list_bili_events()]
        bili_users = [user.to_dict() for user in repository.list_bili_user_emails()]
        return HTMLResponse(_render_index_html(tasks, selected, listener_state, bili_events, bili_users))

    @app.post("/api/tasks")
    def create_task(payload: TaskCreateRequest) -> dict[str, object]:
        """Create a task or return the running duplicate for the same BV id."""
        try:
            task = task_service.submit_task(payload.source)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_id": task.id, "task": task.to_dict()}

    @app.post("/api/video-options")
    def inspect_video_options(payload: VideoOptionsRequest) -> dict[str, object]:
        """Return selectable entries before a task is created."""
        try:
            bvid, title, parts = bili_service.inspect_parts(payload.source)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"bvid": bvid, "title": title, "parts": [part.to_dict() for part in parts]}

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, object]:
        """Return the latest tasks for page polling."""
        return {"tasks": [task.to_dict() for task in task_service.list_tasks()]}

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: int) -> dict[str, object]:
        """Retry one failed task while keeping reusable local artifacts."""
        try:
            task = task_service.retry_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在。") from exc
        return {"task_id": task.id, "task": task.to_dict()}

    @app.get("/api/bili-listener")
    def get_bili_listener() -> dict[str, object]:
        """Return B 站 listener state and recent mention events."""
        bili_event_service.sync_task_statuses()
        return {
            "state": bili_poller.snapshot().to_dict(),
            "events": [event.to_dict() for event in repository.list_bili_events()],
        }

    @app.get("/api/bili-users")
    def list_bili_users() -> dict[str, object]:
        """Return local B 站用户邮箱 bindings."""
        return {"users": [user.to_dict() for user in repository.list_bili_user_emails()]}

    @app.post("/api/bili-users")
    def upsert_bili_user(payload: BiliUserEmailRequest) -> dict[str, object]:
        """Create or update one local B 站用户邮箱 binding."""
        uid = payload.uid.strip()
        email = payload.email.strip()
        if not uid:
            raise HTTPException(status_code=400, detail="请输入 B 站用户 UID。")
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="请输入有效邮箱。")
        user = repository.upsert_bili_user_email(uid, payload.username, email)
        return {"user": user.to_dict()}

    @app.delete("/api/bili-users/{uid}")
    def delete_bili_user(uid: str) -> dict[str, object]:
        """Delete one local B 站用户邮箱 binding."""
        repository.delete_bili_user_email(uid)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> dict[str, object]:
        """Return one task with the full Markdown content and error state."""
        try:
            task = task_service.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在。") from exc
        return {"task": task.to_dict()}

    return app


def _render_index_html(
    tasks: list[dict[str, object]],
    selected: dict[str, object] | None,
    listener_state: dict[str, object],
    bili_events: list[dict[str, object]],
    bili_users: list[dict[str, object]],
) -> str:
    """Render a lightweight HTML page without adding a template dependency."""
    selected_markdown = escape(str((selected or {}).get("markdown_content", "")))
    selected_path = escape(str((selected or {}).get("markdown_file_path", "")))
    selected_error = escape(str((selected or {}).get("error_message", "")))
    selected_title = escape(str((selected or {}).get("video_title", "")))
    selected_status = escape(str((selected or {}).get("status", "")))
    selected_mail_status = escape(str((selected or {}).get("mail_status", "")))
    retry_display = "" if selected_status == TaskStatus.FAILED.value else "display:none;"

    items = []
    for task in tasks:
        task_title = escape(str(task.get("video_title") or task.get("bvid") or f"任务 {task.get('id')}"))
        items.append(
            "\n".join(
                [
                    f'<button class="task-item" data-task-id="{task["id"]}">',
                    f'  <span class="task-title">{task_title}</span>',
                    f'  <span class="task-meta">#{task["id"]} {escape(str(task["status"]))}</span>',
                    "</button>",
                ]
            )
        )
    task_html = "\n".join(items) or '<div class="empty">还没有任务，先提交一个视频。</div>'
    listener_status = escape(str(listener_state.get("login_status", "UNKNOWN")))
    listener_running = "运行中" if listener_state.get("running") else "未运行"
    listener_error = escape(str(listener_state.get("last_error", "")))
    event_html = _render_bili_event_html(bili_events)
    user_html = _render_bili_user_html(bili_users)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>个人版 B 站 AI 助手</title>
  <style>
    :root {{
      --bg: #f3f5f8;
      --panel: #ffffff;
      --line: #d9e0e8;
      --text: #16202a;
      --muted: #5e6b78;
      --accent: #0086ff;
      --accent-soft: #e9f3ff;
      --danger: #cb334d;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: linear-gradient(180deg, #eef4fb 0%, #f7f9fc 100%);
      color: var(--text);
    }}
    .shell {{
      max-width: 1400px;
      margin: 0 auto;
      padding: 24px;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 20px;
      min-height: 100vh;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 30px rgba(17, 31, 44, 0.06);
    }}
    .left {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .right {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
    }}
    .sub {{
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }}
    form {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    input {{
      width: 100%;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 14px;
    }}
    button.submit {{
      border: 0;
      border-radius: 8px;
      padding: 12px 14px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      cursor: pointer;
    }}
    button.secondary {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fff;
      color: var(--text);
      font-size: 14px;
      cursor: pointer;
    }}
    .part-panel {{
      display: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
      gap: 8px;
      flex-direction: column;
    }}
    .part-panel.visible {{
      display: flex;
    }}
    .part-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 260px;
      overflow: auto;
    }}
    .part-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      text-align: left;
      cursor: pointer;
    }}
    .part-item:hover {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .part-title {{
      font-size: 14px;
      font-weight: 600;
      line-height: 1.5;
    }}
    .tasks {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 58vh;
      overflow: auto;
    }}
    .listener {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .event-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 240px;
      overflow: auto;
    }}
    .event-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      text-align: left;
      cursor: pointer;
    }}
    .event-item.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .user-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 220px;
      overflow: auto;
    }}
    .user-item {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 10px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }}
    .delete-user {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--danger);
      padding: 8px 10px;
      cursor: pointer;
    }}
    .task-item {{
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 12px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .task-item.active {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .task-title {{
      font-size: 14px;
      font-weight: 600;
      line-height: 1.5;
    }}
    .task-meta, .meta-line {{
      color: var(--muted);
      font-size: 13px;
    }}
    .empty {{
      color: var(--muted);
      font-size: 14px;
      padding: 12px;
      border: 1px dashed var(--line);
      border-radius: 8px;
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .status-box {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfe;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 14px;
      line-height: 1.7;
      min-height: 420px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #fafcff;
    }}
    .danger {{
      color: var(--danger);
    }}
    @media (max-width: 980px) {{
      .shell {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel left">
      <div>
        <h1>个人版 B 站 AI 助手</h1>
        <div class="sub">输入 BV 号或视频链接，系统会优先抓字幕，没有字幕再下载音频转写，最后生成 Markdown 并发邮件。</div>
      </div>
      <form id="task-form">
        <input id="source-input" name="source" placeholder="例如：BV1xx411c7mD 或 https://www.bilibili.com/video/BV..." />
        <button class="submit" type="submit">解析并创建任务</button>
        <div id="form-message" class="meta-line"></div>
      </form>
      <div id="part-panel" class="part-panel">
        <div class="meta-line" id="part-panel-title">选择要处理的视频条目</div>
        <div id="part-list" class="part-list"></div>
      </div>
      <div class="listener">
        <div class="sub">B 站监听</div>
        <div class="meta-line">状态：<span id="bili-login-status">{listener_status}</span> / <span id="bili-running-status">{listener_running}</span></div>
        <div class="meta-line">账号：<span id="bili-account">暂无</span></div>
        <div class="meta-line">最近轮询：<span id="bili-last-poll">暂无</span></div>
        <div id="bili-error" class="danger">{listener_error or "无"}</div>
        <div id="bili-event-list" class="event-list">{event_html}</div>
      </div>
      <div class="listener">
        <div class="sub">用户邮箱簿</div>
        <form id="bili-user-form">
          <input id="bili-user-uid" name="uid" placeholder="B 站用户 UID，例如 123456" />
          <input id="bili-user-name" name="username" placeholder="用户名，例如 测试用户" />
          <input id="bili-user-email" name="email" placeholder="邮箱，例如 user@example.com" />
          <button class="submit" type="submit">保存绑定</button>
          <div id="bili-user-message" class="meta-line"></div>
        </form>
        <div id="bili-user-list" class="user-list">{user_html}</div>
      </div>
      <div class="sub">最近任务</div>
      <div id="task-list" class="tasks">{task_html}</div>
    </section>
    <section class="panel right">
      <div class="status-grid">
        <div class="status-box">
          <div class="meta-line">视频标题</div>
          <div id="task-title">{selected_title or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">任务状态</div>
          <div id="task-status">{selected_status or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">邮件状态</div>
          <div id="mail-status">{selected_mail_status or "暂无"}</div>
        </div>
        <div class="status-box">
          <div class="meta-line">文件路径</div>
          <div id="task-path">{selected_path or "暂无"}</div>
        </div>
      </div>
      <div>
        <div class="meta-line">错误信息</div>
        <div id="task-error" class="danger">{selected_error or "无"}</div>
        <button id="retry-task-button" class="secondary" type="button" style="margin-top:10px;{retry_display}">重试当前任务</button>
      </div>
      <div>
        <div class="meta-line">Markdown 预览</div>
        <pre id="markdown-preview">{selected_markdown or "暂无结果"}</pre>
      </div>
    </section>
  </div>
  <script>
    let selectedTaskId = {selected["id"] if selected else "null"};

    async function fetchTasks() {{
      const response = await fetch('/api/tasks');
      const payload = await response.json();
      renderTaskList(payload.tasks || []);
      if (selectedTaskId) {{
        await fetchTaskDetail(selectedTaskId);
      }} else if ((payload.tasks || []).length > 0) {{
        selectedTaskId = payload.tasks[0].id;
        await fetchTaskDetail(selectedTaskId);
      }}
    }}

    async function fetchBiliListener() {{
      const response = await fetch('/api/bili-listener');
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      renderBiliListener(payload.state || {{}}, payload.events || []);
    }}

    async function fetchBiliUsers() {{
      const response = await fetch('/api/bili-users');
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      renderBiliUsers(payload.users || []);
    }}

    function renderTaskList(tasks) {{
      const container = document.getElementById('task-list');
      if (!tasks.length) {{
        container.innerHTML = '<div class="empty">还没有任务，先提交一个视频。</div>';
        return;
      }}
      container.innerHTML = tasks.map((task) => {{
        const title = task.video_title || task.bvid || ('任务 ' + task.id);
        const active = task.id === selectedTaskId ? 'active' : '';
        return `
          <button class="task-item ${{active}}" data-task-id="${{task.id}}">
            <span class="task-title">${{escapeHtml(title)}}</span>
            <span class="task-meta">#${{task.id}} ${{escapeHtml(task.status)}}</span>
          </button>
        `;
      }}).join('');
      container.querySelectorAll('[data-task-id]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          selectedTaskId = Number(button.dataset.taskId);
          await fetchTaskDetail(selectedTaskId);
          renderTaskList(tasks);
        }});
      }});
    }}

    function renderBiliListener(state, events) {{
      document.getElementById('bili-login-status').textContent = state.login_status || 'UNKNOWN';
      document.getElementById('bili-running-status').textContent = state.running ? '运行中' : '未运行';
      document.getElementById('bili-account').textContent = state.account_name
        ? `${{state.account_name}} (${{state.account_mid || 'unknown'}})`
        : '暂无';
      document.getElementById('bili-last-poll').textContent = state.last_poll_at || '暂无';
      document.getElementById('bili-error').textContent = state.last_error || '无';

      const container = document.getElementById('bili-event-list');
      if (!events.length) {{
        container.innerHTML = '<div class="empty">暂无 B 站 @ 事件。</div>';
        return;
      }}
      container.innerHTML = events.slice(0, 10).map((event) => {{
        const taskId = event.task_id || '';
        const active = taskId && Number(taskId) === selectedTaskId ? 'active' : '';
        return `
          <button class="event-item ${{active}}" type="button" data-task-id="${{taskId}}">
            <div class="task-title">${{escapeHtml(event.sender_name || event.sender_mid || '未知用户')}}</div>
            <div class="task-meta">${{escapeHtml(event.status || '')}} / ${{escapeHtml(event.delivery_status || '')}} #${{escapeHtml(event.bvid || '')}}</div>
            <div class="task-meta">UID ${{escapeHtml(event.sender_mid || '')}} · ${{escapeHtml(event.recipient_email || '未绑定邮箱')}}</div>
            <div class="task-meta">${{escapeHtml(event.content || '')}}</div>
          </button>
        `;
      }}).join('');
      container.querySelectorAll('[data-task-id]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          if (!button.dataset.taskId) {{
            return;
          }}
          selectedTaskId = Number(button.dataset.taskId);
          await fetchTaskDetail(selectedTaskId);
        }});
      }});
    }}

    function renderBiliUsers(users) {{
      const container = document.getElementById('bili-user-list');
      if (!users.length) {{
        container.innerHTML = '<div class="empty">暂无用户邮箱绑定。</div>';
        return;
      }}
      container.innerHTML = users.map((user) => `
        <div class="user-item">
          <div>
            <div class="task-title">${{escapeHtml(user.username || '未命名用户')}}</div>
            <div class="task-meta">UID ${{escapeHtml(user.uid)}} · ${{escapeHtml(user.email)}}</div>
          </div>
          <button class="delete-user" type="button" data-uid="${{escapeHtml(user.uid)}}">删除</button>
        </div>
      `).join('');
      container.querySelectorAll('[data-uid]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          await fetch(`/api/bili-users/${{encodeURIComponent(button.dataset.uid)}}`, {{ method: 'DELETE' }});
          await fetchBiliUsers();
        }});
      }});
    }}

    async function fetchTaskDetail(taskId) {{
      const response = await fetch(`/api/tasks/${{taskId}}`);
      if (!response.ok) {{
        return;
      }}
      const payload = await response.json();
      const task = payload.task;
      document.getElementById('task-title').textContent = task.video_title || task.bvid || '暂无';
      document.getElementById('task-status').textContent = task.status || '暂无';
      document.getElementById('mail-status').textContent = task.mail_status || '暂无';
      document.getElementById('task-path').textContent = task.markdown_file_path || '暂无';
      document.getElementById('task-error').textContent = task.error_message || '无';
      document.getElementById('markdown-preview').textContent = task.markdown_content || '暂无结果';
      document.getElementById('retry-task-button').style.display = task.status === 'FAILED' ? 'inline-block' : 'none';
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    async function createTaskFromSource(source) {{
      const message = document.getElementById('form-message');
      message.textContent = '任务已提交，正在创建。';
      const response = await fetch('/api/tasks', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ source }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '提交失败。';
        return;
      }}
      selectedTaskId = payload.task_id;
      document.getElementById('source-input').value = '';
      hidePartPanel();
      message.textContent = `任务 #${{payload.task_id}} 已创建。`;
      await fetchTasks();
    }}

    function renderPartOptions(title, parts) {{
      const panel = document.getElementById('part-panel');
      const panelTitle = document.getElementById('part-panel-title');
      const list = document.getElementById('part-list');
      panelTitle.textContent = `${{title}}：请选择要处理的视频条目`;
      list.innerHTML = parts.map((part) => `
        <button class="part-item" type="button" data-url="${{escapeHtml(part.url)}}">
          <div class="part-title">P${{part.index}} ${{escapeHtml(part.title)}}</div>
          <div class="task-meta">${{formatDuration(part.duration)}} · 点击后创建这个条目的总结任务</div>
        </button>
      `).join('');
      list.querySelectorAll('[data-url]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          await createTaskFromSource(button.dataset.url);
        }});
      }});
      panel.classList.add('visible');
    }}

    function hidePartPanel() {{
      document.getElementById('part-panel').classList.remove('visible');
      document.getElementById('part-list').innerHTML = '';
    }}

    function formatDuration(seconds) {{
      const total = Number(seconds || 0);
      if (!total) {{
        return '时长未知';
      }}
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return `${{minutes}}:${{String(rest).padStart(2, '0')}}`;
    }}

    document.getElementById('task-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const source = document.getElementById('source-input').value.trim();
      const message = document.getElementById('form-message');
      if (!source) {{
        message.textContent = '请输入 BV 号或视频链接。';
        return;
      }}

      hidePartPanel();
      message.textContent = '正在解析视频条目。';
      const response = await fetch('/api/video-options', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ source }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '解析失败。';
        return;
      }}
      const parts = payload.parts || [];
      if (parts.length > 1) {{
        message.textContent = `检测到 ${{parts.length}} 个条目，请选择其中一个。`;
        renderPartOptions(payload.title || payload.bvid || '视频合集', parts);
        return;
      }}
      await createTaskFromSource(parts[0]?.url || source);
    }});

    document.getElementById('bili-user-form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const message = document.getElementById('bili-user-message');
      const uid = document.getElementById('bili-user-uid').value.trim();
      const username = document.getElementById('bili-user-name').value.trim();
      const email = document.getElementById('bili-user-email').value.trim();
      if (!uid || !email) {{
        message.textContent = '请输入 UID 和邮箱。';
        return;
      }}
      const response = await fetch('/api/bili-users', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ uid, username, email }})
      }});
      const payload = await response.json();
      if (!response.ok) {{
        message.textContent = payload.detail || '保存失败。';
        return;
      }}
      document.getElementById('bili-user-uid').value = '';
      document.getElementById('bili-user-name').value = '';
      document.getElementById('bili-user-email').value = '';
      message.textContent = '绑定已保存。';
      await fetchBiliUsers();
    }});

    document.getElementById('retry-task-button').addEventListener('click', async () => {{
      if (!selectedTaskId) {{
        return;
      }}
      const response = await fetch(`/api/tasks/${{selectedTaskId}}/retry`, {{ method: 'POST' }});
      if (response.ok) {{
        await fetchTaskDetail(selectedTaskId);
        await fetchTasks();
      }}
    }});

    fetchTasks();
    fetchBiliListener();
    fetchBiliUsers();
    setInterval(async () => {{
      await fetchTasks();
      await fetchBiliListener();
      await fetchBiliUsers();
    }}, 4000);
  </script>
</body>
</html>"""


def _render_bili_event_html(events: list[dict[str, object]]) -> str:
    """Render recent B 站 events for the first page load."""
    if not events:
        return '<div class="empty">暂无 B 站 @ 事件。</div>'

    items = []
    for event in events[:10]:
        sender = escape(str(event.get("sender_name") or event.get("sender_mid") or "未知用户"))
        content = escape(str(event.get("content") or ""))
        status = escape(str(event.get("status") or ""))
        delivery_status = escape(str(event.get("delivery_status") or ""))
        sender_mid = escape(str(event.get("sender_mid") or ""))
        recipient_email = escape(str(event.get("recipient_email") or "未绑定邮箱"))
        task_id = escape(str(event.get("task_id") or ""))
        data_task = f' data-task-id="{task_id}"' if task_id else ""
        items.append(
            "\n".join(
                [
                    f'<button class="event-item"{data_task} type="button">',
                    f'  <div class="task-title">{sender}</div>',
                    f'  <div class="task-meta">{status} / {delivery_status}</div>',
                    f'  <div class="task-meta">UID {sender_mid} · {recipient_email}</div>',
                    f'  <div class="task-meta">{content}</div>',
                    "</button>",
                ]
            )
        )
    return "\n".join(items)


def _render_bili_user_html(users: list[dict[str, object]]) -> str:
    """Render the local UID to email bindings for the first page load."""
    if not users:
        return '<div class="empty">暂无用户邮箱绑定。</div>'

    items = []
    for user in users[:20]:
        uid = escape(str(user.get("uid") or ""))
        username = escape(str(user.get("username") or "未命名用户"))
        email = escape(str(user.get("email") or ""))
        items.append(
            "\n".join(
                [
                    '<div class="user-item">',
                    "  <div>",
                    f'    <div class="task-title">{username}</div>',
                    f'    <div class="task-meta">UID {uid} · {email}</div>',
                    "  </div>",
                    f'  <button class="delete-user" type="button" data-uid="{uid}">删除</button>',
                    "</div>",
                ]
            )
        )
    return "\n".join(items)


app = build_app()
