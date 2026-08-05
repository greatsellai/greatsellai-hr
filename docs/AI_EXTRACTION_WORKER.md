# AI 简历提取后台任务

上传 API 与 `reparse-source` 修复 API 都不会等待模型响应，也不会在 API 进程中执行 PDF、LibreOffice、OpenPyXL 或腾讯 OCR。它们只做受限大小与文件签名/哈希校验、原件原子落盘或安全复制，并在同一数据库事务中创建文档解析任务。

worker 先领取 `resume_document_extraction_jobs`：在对应工作区内再次校验原文件路径、SHA-256 和资源上限，然后把 PDF/DOC/DOCX/XLS/XLSX/PNG/JPG/JPEG/HTML/HTM 标准化为带页码的原文块。只有标准化完成且存在原文块时，worker 才会创建后续 AI 提取任务。浏览器不传递、也不会获得模型密钥。

## 状态

文档解析任务：

- `queued`：原件已安全保存，等待受控 worker 解析。
- `running`：worker 已获得租约；API 仍可提供登录、筛选和原文件访问，不等待解析完成。
- `completed`：原文块已持久化；如有可用原文，AI 提取任务已在同一提交中创建。
- `needs_attention`：原件哈希不一致、格式不合法、超出页数/文本/表格/压缩展开上限，或重试耗尽。简历的 `extraction_status` 会是 `failed` 并返回稳定错误码，方便重新上传。

- `queued`：已入队，等待 worker。
- `running`：worker 已获得租约并正在调用模型。
- `completed`：结构化事实已通过字段级原文证据校验并写入，简历为 `ready` 且 `is_active=true`，可立即参与筛选。
- `needs_attention`：模型结果无可用字段、网络重试耗尽或数据校验失败；可在上传页重新入队或重新上传 PDF。
- `unavailable`：服务端没有任何可用的模型凭据路径，未发起模型请求。旧 DeepSeek 路径读取 `DEEPSEEK_API_KEY`；平台 Provider 路径按 `credential_ref` 读取 `RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON`。若已发布的历史路由引用无法解析，Gateway 会以 `ai_route_credential_not_configured` 安全失败，不会访问上游。

扫描件、加密件或原生文字质量不合格的文件会先进入文档 worker；无法得到可靠文字时不会创建 AI 提取任务。原件仍保留在所属工作区，便于重新上传或查看失败原因。

## 本地运行

API 与 worker 分别启动：

```powershell
uvicorn app.main:app --reload
python -m app.ai_extraction_worker
```

只处理一条任务后退出可使用：

```powershell
python -m app.ai_extraction_worker --once
```

## 云端部署

`compose.yml` 已包含 `worker` 服务，会在数据库迁移和院校名册初始化完成后启动。它与 API 使用同一份服务端环境变量：旧 DeepSeek 路径使用 `DEEPSEEK_API_KEY`，平台 Provider 使用 `RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON` 的引用映射。不要把任一密钥放入前端、请求体、数据库或审计日志；`credential_ref` 只是一把非秘密的查找键。

可选运行参数：

- `RESUME_V3_AI_EXTRACTION_JOB_MAX_ATTEMPTS`：单任务自动重试上限，默认 `3`。
- `RESUME_V3_AI_EXTRACTION_JOB_LEASE_SECONDS`：worker 租约，默认 `180`；必须至少比 `DEEPSEEK_TIMEOUT_SECONDS` 多 30 秒。
- `RESUME_V3_AI_EXTRACTION_WORKER_POLL_SECONDS`：空队列轮询间隔，默认 `2`。
- `RESUME_V3_DOCUMENT_EXTRACTION_JOB_MAX_ATTEMPTS`：文档解析自动重试上限，默认 `3`。
- `RESUME_V3_DOCUMENT_EXTRACTION_JOB_LEASE_SECONDS`：文档解析 worker 租约，默认 `180`；必须比最长 Office 或腾讯 OCR 操作超时多至少 30 秒。
- `RESUME_V3_DOCUMENT_MAX_PAGES`：PDF 或 Office 转换后最大页数，默认 `30`。
- `RESUME_V3_DOCUMENT_MAX_TEXT_CHARS`：全部标准化原文最大字符数，默认 `250000`。
- `RESUME_V3_DOCUMENT_MAX_ARCHIVE_UNCOMPRESSED_BYTES`：DOCX/XLSX 解压展开最大字节数，默认 `104857600`。
- `RESUME_V3_DOCUMENT_MAX_SPREADSHEET_SHEETS`、`RESUME_V3_DOCUMENT_MAX_SPREADSHEET_ROWS_PER_SHEET`、`RESUME_V3_DOCUMENT_MAX_SPREADSHEET_CELLS`：表格资源上限，默认 `20`、`5000`、`50000`。
- `RESUME_V3_DOCUMENT_OFFICE_TIMEOUT_SECONDS`：Office 转换子进程硬超时，默认 `90` 秒。
- `TENCENT_OCR_TIMEOUT_SECONDS`：腾讯 OCR 请求超时，默认 `20` 秒。
- `TENCENT_OCR_API`：腾讯 OCR Action。未设置时保持历史的 `GeneralBasicOCR`；显式设为 `GeneralAccurateOCR` 可使用高精度识别，适合文字较多、版式复杂或难以识别的简历。生产模板显式选择高精度版，staging 必须复制生产的同一选择。两种 Action 都只提交内存中的 Tencent Base64 请求，不发布原件 URL；图片上传会先校验腾讯凭据，大图只在受控像素上限内本地压缩，超限会以稳定错误码待处理。生产镜像不再内置 Tesseract，也不再支持本地图片 OCR 回退。

worker 以数据库租约领取任务，因此重启、重复启动或短暂网络失败不会让两个 worker 同时写入同一份简历；过期租约会被安全回收，已自动启用的新版本不会被旧任务覆盖。所有原文件路径、队列行和写回记录都按已领取的工作区重新绑定；即使数据库存在异常的跨工作区外键引用，worker 也只会终止自己工作区的任务，不会读取另一工作区的简历。

## 并发与工作区公平性

Worker 不会按用户或工作区创建常驻容器。生产使用一个共享 worker 容器，并可在其中启动多个隔离子进程。每个子进程都有自己的数据库引擎、连接池、心跳和任务租约，因此一个慢的 IMAP、Office、腾讯 OCR 或模型调用不会让所有其他工作区停止处理。

重任务队列会在领取任务时取得同一个工作区的短期逻辑槽位，覆盖邮箱同步、文档解析、AI 事实提取、姓名补全、自动总结、JD 批量匹配和批量评分。同一个工作区在任意时刻最多运行一个这类重任务；其他空闲子进程会按“最近最少服务”的工作区顺序领取任务。进程崩溃时槽位随租约到期自动回收，邮箱 IMAP 心跳会同时续期任务租约和工作区槽位。

文档解析在逐页腾讯 OCR 期间也会以独立短会话同时续期任务与工作区槽位；若进程崩溃，任务恢复会按记录的任务标识清除对应失效槽位，不会因为管理员配置了较长的槽位租约而把该工作区卡住。

可选运行参数：

- `RESUME_V3_WORKER_CONCURRENCY`：worker 容器内的子进程数，默认 `1`。大于 `1` 只允许 PostgreSQL，且必须关闭 worker 自建表和院校播种，迁移与播种先由 `migrate` 服务完成。
- `RESUME_V3_WORKER_DATABASE_POOL_SIZE`、`RESUME_V3_WORKER_DATABASE_MAX_OVERFLOW`：每个子进程的独立数据库连接池，默认 `1`、`0`。总预算上限为 `32` 个连接，避免扩容时挤占 API 连接。
- `RESUME_V3_WORKER_WORKSPACE_LANE_LEASE_SECONDS`：工作区重任务槽位的最短租约，默认 `210` 秒。具体任务租约更长时自动取更长值；邮箱任务在 IMAP 心跳时续期。

建议先在 staging 以 `1` 验证，再在生产从 `2` 开始观察“在线 / 已配置”进程数、队列等待时间和数据库连接数。不要使用 `docker compose --scale worker`，现有发布恢复流程按一个 worker 容器设计；本功能通过容器内部的受控子进程扩容。
