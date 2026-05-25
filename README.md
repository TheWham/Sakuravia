# 个人版 B 站 AI 助手 V1

## 功能

- 输入 `BV` 号或 B 站视频链接，创建本地摘要任务
- 优先读取字幕，无字幕时下载音频并调用 Groq Whisper 转写
- 调用 DeepSeek 生成 Markdown 总结
- 将 Markdown 落盘，并作为邮件附件发送到固定邮箱
- 页面内轮询显示任务状态和结果预览
- 可选开启 B 站 AI 助手账号的 `@我` 通知监听，评论中包含 BV/视频链接时自动创建或复用总结任务
- 可在本地页面维护 B 站用户 UID 到邮箱的绑定，@ 触发结果会发送到请求用户绑定邮箱

## 运行要求

- Python 3.11+
- `yt-dlp`
- `ffmpeg`
- 可用的 Groq API Key
- 可用的 DeepSeek API Key
- 可用的 SMTP 账号
- 可选：B 站 AI 助手账号 Cookie 和账号 MID，用于 V2 `@我` 监听

## 快速开始

1. 复制 `.env.example` 为 `.env` 并填写配置
2. 启动服务

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. 打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)

## B 站 @ 监听

V2 监听默认关闭。需要使用时，在 `.env` 中配置：

```dotenv
BILI_ENABLE_LISTENER=true
BILI_COOKIE=浏览器里复制出来的 AI 助手 B 站账号完整 Cookie
BILI_SELF_MID=AI 助手 B 站账号 MID
BILI_POLL_INTERVAL_SECONDS=60
BILI_REQUEST_TIMEOUT_SECONDS=15
```

开启后重启服务。别人评论里 `@AI助手账号 BVxxxx` 或 `@AI助手账号 https://www.bilibili.com/video/BV...` 时，系统会在本地记录事件并创建或复用任务。邮件收件人来自本地“用户邮箱簿”中该评论用户 UID 对应的邮箱；未绑定邮箱的用户不会创建任务。当前版本只发邮件和本地展示，不自动回复 B 站评论。

## 测试

```powershell
python -m unittest discover -s tests -v
```
