# 个人版 B 站 AI 助手 V1

## 功能

- 输入 `BV` 号或 B 站视频链接，创建本地摘要任务
- 优先读取字幕，无字幕时下载音频并调用 Groq Whisper 转写
- 调用 DeepSeek 生成 Markdown 总结
- 将 Markdown 落盘，并作为邮件附件发送到固定邮箱
- 页面内轮询显示任务状态和结果预览

## 运行要求

- Python 3.11+
- `yt-dlp`
- `ffmpeg`
- 可用的 Groq API Key
- 可用的 DeepSeek API Key
- 可用的 SMTP 账号

## 快速开始

1. 复制 `.env.example` 为 `.env` 并填写配置
2. 启动服务

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. 打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)

## 测试

```powershell
python -m unittest discover -s tests -v
```
