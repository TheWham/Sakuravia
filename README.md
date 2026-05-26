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

## AI 总结模板

V3 使用 DeepSeek 的 OpenAI 兼容接口生成 Markdown，总结模板在 `app/services/summary.py` 中维护。模板目标不是简单复述转写稿，而是把视频整理成适合手机邮箱阅读、可以长期收藏的中文笔记。

### DeepSeek 配置模板

```dotenv
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_MODEL=deepseek-chat
```

### 单段视频总结模板

当转写内容较短时，系统会把视频元信息和完整转写一次性提交给 DeepSeek。模板要求如下：

```text
系统角色：
你是一名严谨的中文视频内容整理助手。

用户提示词：
请根据提供的视频元信息和转写内容，输出一份适合直接发到手机邮箱阅读、可以长期收藏的 Markdown 中文笔记。

必须遵守：
1. 只输出 Markdown 正文，不要输出额外说明。
2. 先判断内容类型，再选择笔记结构：清单/Prompt类、技术实践类、教程步骤类、观点解读类。
3. 不要像转写稿复述，要像人工整理的专题笔记：提炼主题、归类、补充可执行用途。
4. 标题改写成主题型标题，不要机械使用“视频标题”。
5. 对视频明确说出的事实保持忠实；用途、场景、建议可以合理推断，但不能编造具体数据、链接、评论、人物或来源。
6. 清单、盘点、TopN、Prompt 合集、工具合集优先输出“核心清单表”。
7. 技术实践或原理讲解要提炼背景问题、核心原理、关键数据、最佳实践、反例与坑点、核心原则总结。
8. 教程步骤要输出步骤表、注意事项、可执行清单。
9. 短视频也不能写空，至少给出清单表、场景分组、使用建议。

推荐结构：
清单/Prompt类：
- # 主题型标题
- ## 📌 视频信息
- ## 📊 核心清单表
- ## 🧭 应用场景总结
- ## ✅ 使用建议
- ## 💡 AI 理解与延伸

技术实践类：
- # 主题型标题
- ## 📌 视频信息
- ## 🎯 背景问题
- ## 🧠 核心原理
- ## 📊 关键数据
- ## ✅ 最佳实践
- ## ⚠️ 反例与坑点
- ## 🧩 核心原则总结
- ## 🚀 可执行建议
- ## 💡 AI 理解与延伸

教程步骤类：
- # 主题型标题
- ## 📌 视频信息
- ## 📊 操作步骤表
- ## ⚠️ 注意事项
- ## ✅ 可执行清单
- ## 🚀 延伸建议
- ## 💡 AI 理解与延伸

视频信息占位：
视频标题：{title}
BV号：{bvid}
UP主：{uploader}
时长（秒）：{duration}
标签：{tags}
视频简介：{description}

转写内容：
{transcript}
```

### 长视频合并模板

转写内容较长时，系统会先按段生成局部笔记，再把多段笔记合并成最终 Markdown。合并模板关注去重、保留事实、统一结构：

```text
请将同一个视频的多段笔记合并成一份最终 Markdown 邮件正文。

要求：
1. 只输出 Markdown。
2. 开头直接进入主题，不解释“我将合并”。
3. 去掉重复表达，保留事实；用途、适用场景、建议可以基于标题、简介、标签、条目名称做合理常识推断。
4. 如果分段笔记里出现清单、Prompt、工具、步骤或对比，最终结果必须保留或生成 Markdown 表格。
5. 标题改写成主题型标题，不要用“视频标题”。
6. 技术实践类最终稿必须包含：📌 视频信息、🎯 背景问题、🧠 核心原理、📊 关键数据、✅ 最佳实践、⚠️ 反例与坑点、🧩 核心原则总结、🚀 可执行建议、💡 AI 理解与延伸。
7. 清单类最终稿必须包含：📌 视频信息、📊 核心清单表、🧭 应用场景总结、✅ 使用建议、💡 AI 理解与延伸。
8. “💡 AI 理解与延伸”必须和视频事实分开，避免把推断写成原视频事实。

视频标题：{title}
BV号：{bvid}
UP主：{uploader}
标签：{tags}
视频简介：{description}

分段摘要片段：
{chunks}
```

## 邮箱模板

V3 邮件由 `app/services/mail.py` 生成。系统会同时发送纯文本正文、HTML 正文和 Markdown 附件，避免不同邮箱客户端显示不一致时看不到完整内容。

### SMTP 配置模板

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=465
SMTP_USERNAME=你的 SMTP 登录账号
SMTP_PASSWORD=你的 SMTP 授权码或密码
SMTP_USE_SSL=true
MAIL_FROM=发件邮箱地址
MAIL_TO=默认收件邮箱地址
```

说明：

- `SMTP_USE_SSL=true` 时使用 `SMTP_SSL`，常见端口是 `465`。
- `SMTP_USE_SSL=false` 时会使用 `STARTTLS`，常见端口是 `587`。
- 页面提交的普通任务默认发送到 `MAIL_TO`。
- B 站 `@我` 触发的任务会发送到本地“用户邮箱簿”中对应 UID 绑定的邮箱。
- 很多邮箱不能直接使用登录密码，需要在邮箱后台生成 SMTP 授权码。

### 邮件标题模板

```text
B站视频总结 - {视频标题}
```

### 纯文本正文模板

```text
视频《{视频标题}》的 Markdown 总结如下，附件中也保留了一份 .md 文件。

{AI 生成的 Markdown 总结正文}
```

### HTML 正文模板

HTML 正文会把 AI 生成的 Markdown 转成适合移动端邮箱阅读的排版，当前支持：

- `#`、`##`、`###` 标题
- 无序列表和有序列表
- Markdown 表格
- `**加粗**` 和行内代码
- 按固定符号给重点段落加浅色块，例如 `⚠️`、`✅`、`🚀`、`💡`、`🧠`

渲染骨架如下：

```html
<!doctype html>
<html lang="zh-CN">
<body>
  <div style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Microsoft YaHei, sans-serif; font-size: 16px; line-height: 1.75; color: #1f2933; padding: 8px;">
    <p style="margin: 0 0 20px; color: #52606d;">视频《{视频标题}》的总结如下，附件中也保留了一份 .md 文件。</p>
    {AI 生成的 Markdown 转换后的 HTML 内容}
  </div>
</body>
</html>
```

### 附件模板

每封邮件都会附带一份 Markdown 文件，文件内容与正文中的 AI 总结一致。附件命名由落盘逻辑生成，默认保存在 `data/output/` 下，方便后续本地归档和重新发送。

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

## 许可与使用限制

本项目不是 MIT 许可，也不是开放商用许可。项目仅供作者个人学习、研究和本机自用，未经作者书面许可不得用于商业用途、对外付费服务、SaaS 服务、商业产品集成、转售分发或违反第三方平台规则的批量处理场景。

完整条款见 [LICENSE](LICENSE)。
