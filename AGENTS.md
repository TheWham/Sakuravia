# AGENTS.md

## 项目目标

这是一个仅供本机个人使用的 `B 站 AI 助手`。当前版本目标很明确：

- 提供本地网页
- 手动输入 `BV` 号或 B 站视频链接
- 优先读取字幕
- 无字幕时下载音频并调用 `Groq Whisper`
- 调用 `DeepSeek` 生成 Markdown 总结
- 将 `.md` 文件落盘
- 通过 `SMTP` 把 `.md` 作为附件发送到固定邮箱
- 可选开启 B 站 AI 助手账号的 `@我` 通知监听，评论中带 BV/链接时自动创建或复用任务
- 支持本地维护 B 站用户 UID 到邮箱的绑定，@ 触发结果发给请求用户绑定邮箱

当前版本**不做**这些事情：

- 不做用户系统
- 不做局域网开放
- 不做多任务并发优化
- 不做 Redis、MQ、独立 Worker
- 不做浏览器自动化
- 不做 B 站扫码登录
- 不自动回复 B 站评论

## 当前实现基线

当前工作区已经建立了第一版代码骨架，目录结构如下：

- `app/`
  - `main.py`：FastAPI 入口、页面渲染、HTTP 接口
  - `config.py`：`.env` 配置读取
  - `models.py`：任务状态、邮件状态、核心数据模型
  - `storage.py`：SQLite 持久化
  - `services/`
    - `bili.py`：BV 解析、视频元信息获取、字幕提取
    - `audio.py`：音频下载与切片
    - `transcription.py`：Groq Whisper 转写
    - `summary.py`：DeepSeek Markdown 总结
    - `artifact.py`：Markdown 落盘
    - `mail.py`：SMTP 发信
    - `task_service.py`：任务编排与状态流转
    - `bili_listener.py`：B 站 Cookie 鉴权、`@我` 轮询、事件入库、任务触发
- `tests/`
  - 当前已覆盖输入解析、文件命名、任务状态流、邮件组装、B 站事件处理

## 固定技术路线

后续默认沿着这条路线演进，除非用户明确要求调整：

- `Python 3.11+`
- `FastAPI`
- `SQLite`
- 进程内 `ThreadPoolExecutor(max_workers=1)`
- `yt-dlp`
- `ffmpeg`
- `Groq Whisper`
- `DeepSeek`
- `SMTP`

不要擅自切成这些替代方案：

- 不要改成 Java / Spring Boot
- 不要改成前后端分离
- 不要默认接入数据库中间件或缓存
- 不要先做 B 站账号监听链路

## 运行依赖与默认约定

运行前需要准备：

- `.env`
- `yt-dlp`
- `ffmpeg`
- Groq API Key
- DeepSeek API Key
- SMTP 账号
- 可选：AI 助手 B 站账号 Cookie 和 `BILI_SELF_MID`

`.env.example` 已给出字段模板。默认约定如下：

- 服务仅监听 `127.0.0.1`
- SQLite 默认在 `data/tasks.db`
- Markdown 输出目录默认在 `data/output/`
- 音频目录默认在 `data/audio/`
- 临时目录默认在 `data/tmp/`

## 代码约束

后续改动请继续遵守这些约束：

- 关键业务方法保留详细但克制的注释和 docstring
- 先保持“字幕优先，音频转写兜底”的处理顺序
- 同一个 `bvid` 在运行中禁止重复创建新任务
- 邮件接收人维持固定配置，不从页面动态输入
- 错误信息要落库并在页面可见，不能静默吞错
- 页面维持单页形态：输入区、任务列表、结果预览区
- B 站监听默认关闭；Cookie 只走 `.env`，不要在页面提供输入框或展示明文 Cookie
- V2 只做本地记录和邮件通知，不自动回复 B 站评论
- `BILI_COOKIE` 和 `BILI_SELF_MID` 属于 AI 助手 B 站账号；`bili_event.sender_mid` 才是请求用户 UID
- B 站 @ 触发必须先在本地邮箱簿绑定请求用户 UID，否则事件失败且不创建总结任务

## 后续扩展顺序

如果后面继续做，优先级按这个顺序推进：

1. 先验证 V2 的 AI 助手账号 Cookie、`@我` 通知、UID 邮箱绑定和邮件链路
2. 补齐对真实 B 站字幕 / 无字幕视频的集成测试
3. 优化长文本分块摘要和异常提示
4. 再考虑评论回复、扫码登录或指定视频评论区监听

## 上下文维护建议

以后完成关键任务后，建议把这些信息同步到本文件，而不是只留在对话里：

- 当前版本做到了什么
- 哪些依赖路径在这台机器上可用
- 哪些外部 API 已验证可用
- 哪些问题已经踩过坑
- 下一步最合理的演进方向

这样后续代理接手时，可以先读 `AGENTS.md`，再读代码。
