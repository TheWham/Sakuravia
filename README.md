# 个人版 B 站 AI 助手 V3

## 功能

- 输入 `BV` 号或 B 站视频链接，创建本地摘要任务
- 优先读取字幕，无字幕时下载音频并调用可切换 ASR 转写，V3 默认阿里云百炼 Paraformer
- 调用 DeepSeek 生成 Markdown 总结
- 将 Markdown 落盘，并作为邮件附件发送到固定邮箱
- 页面内轮询显示任务状态和结果预览
- 可选开启 B 站 AI 助手账号的 `@我` 通知监听，评论中包含 BV/视频链接时自动创建或复用总结任务
- 可在本地页面维护 B 站用户 UID 到邮箱的绑定，@ 触发结果会发送到请求用户绑定邮箱

## 运行要求

- Python 3.11+
- `yt-dlp`
- `ffmpeg`
- 可用的阿里云百炼 API Key（Paraformer 录音文件识别，需使用中国内地（北京）地域的 API Key）
- 可用的阿里云 OSS Bucket（V3 默认用私有 Bucket + 签名 URL 让 Paraformer 读取临时音频）
- 可选：可用的 Groq API Key（当 `ASR_PROVIDER=groq` 时使用）
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

## 阿里云临时部署

如果要先部署到阿里云 ECS 并通过公网 IP 临时访问，请按 [阿里云 ECS 公网 IP 临时部署手册](docs/deploy/alicloud-public-ip.md) 操作。

关键边界：

- FastAPI 继续只监听 `127.0.0.1:8000`
- Nginx 监听 `80`，开启 Basic Auth 后再反代到 FastAPI
- 阿里云安全组只放行你的公网 IP 到 `22/tcp` 和 `80/tcp`
- 不迁移本机历史任务数据，服务器从空 SQLite 开始
- 后续买域名并解析到中国内地 ECS 前，先处理 ICP 备案和 HTTPS
- 如果 ECS 上解析 B 站返回 `HTTP 412`，配置 `YT_DLP_COOKIES_FILE=data/bilibili-cookies.txt`

## ASR 配置

V3 默认使用阿里云百炼 `paraformer-v2`，适合部署在大陆阿里云服务器上。该模型不接受本地文件直传，系统会把无字幕视频的音频临时上传到私有 OSS Bucket，生成短时有效的签名 URL 提交给百炼，识别完成后尽量删除 OSS 临时对象。

```dotenv
ASR_PROVIDER=aliyun_paraformer
ALIYUN_DASHSCOPE_API_KEY=中国内地（北京）地域的百炼 API Key
ALIYUN_ASR_MODEL=paraformer-v2
ALIYUN_OSS_ACCESS_KEY_ID=
ALIYUN_OSS_ACCESS_KEY_SECRET=
ALIYUN_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
ALIYUN_OSS_BUCKET=
ALIYUN_OSS_PUBLIC_BASE_URL=
ALIYUN_OSS_SIGNED_URL_EXPIRES_SECONDS=3600
ALIYUN_ASR_POLL_INTERVAL_SECONDS=5
ALIYUN_ASR_TIMEOUT_SECONDS=1800
```

`ALIYUN_OSS_PUBLIC_BASE_URL` 留空时会使用签名 URL，Bucket 可以保持私有；只有你明确要用公共读 Bucket 时才填写这个公网域名。

服务器磁盘较小时建议保留默认值：

```dotenv
KEEP_AUDIO_AFTER_SUCCESS=false
```

任务成功后会删除本地音频，失败任务仍会保留音频，方便重试和排查。

如需临时切回 Groq：

```dotenv
ASR_PROVIDER=groq
GROQ_API_KEY=
GROQ_ASR_MODEL=whisper-large-v3-turbo
```

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
