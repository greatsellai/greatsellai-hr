# AI 简历提取后台任务

上传 API 与 `reparse-source` 修复 API 都不会等待模型响应，也不会在 API 进程中执行 PDF、LibreOffice、Tesseract、OpenPyXL 或 OCR。它们只做受限大小与文件签名/哈希校验、原件原子落盘或安全复制，并在同一数据库事务中创建文档解析任务。

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
- `RESUME_V3_DOCUMENT_EXTRACTION_JOB_LEASE_SECONDS`：文档解析 worker 租约，默认 `180`；必须比最长 Office、OCR 或腾讯 OCR 超时多至少 30 秒。
- `RESUME_V3_DOCUMENT_MAX_PAGES`：PDF 或 Office 转换后最大页数，默认 `30`。
- `RESUME_V3_DOCUMENT_MAX_TEXT_CHARS`：全部标准化原文最大字符数，默认 `250000`。
- `RESUME_V3_DOCUMENT_MAX_ARCHIVE_UNCOMPRESSED_BYTES`：DOCX/XLSX 解压展开最大字节数，默认 `104857600`。
- `RESUME_V3_DOCUMENT_MAX_SPREADSHEET_SHEETS`、`RESUME_V3_DOCUMENT_MAX_SPREADSHEET_ROWS_PER_SHEET`、`RESUME_V3_DOCUMENT_MAX_SPREADSHEET_CELLS`：表格资源上限，默认 `20`、`5000`、`50000`。
- `RESUME_V3_DOCUMENT_OFFICE_TIMEOUT_SECONDS`、`RESUME_V3_DOCUMENT_IMAGE_OCR_TIMEOUT_SECONDS`：Office 和图片 OCR 的子进程硬超时，默认 `90`、`60` 秒。

worker 以数据库租约领取任务，因此重启、重复启动或短暂网络失败不会让两个 worker 同时写入同一份简历；过期租约会被安全回收，已自动启用的新版本不会被旧任务覆盖。所有原文件路径、队列行和写回记录都按已领取的工作区重新绑定；即使数据库存在异常的跨工作区外键引用，worker 也只会终止自己工作区的任务，不会读取另一工作区的简历。
