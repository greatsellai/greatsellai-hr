# 简历筛选 V3

这是一个独立重写项目。旧系统和旧的 resume_text 不会被导入为 V3 的可筛选事实。

- [产品需求文档](docs/PRD.md)
- [全项目实施计划](docs/IMPLEMENTATION_PLAN.md)
- [AI 提取后台任务](docs/AI_EXTRACTION_WORKER.md)
- [条件筛选 V2 规则与接口](docs/FILTER_V2.md)
- [团队共建工作流](docs/TEAM_WORKFLOW.md)
- [GitHub Actions CI/CD](docs/CI_CD.md)

当前版本已覆盖：重新上传 PDF → 解析质量校验 → AI 从原文识别候选人姓名（不可靠则留空）并提取教育/经历/技能 → 字段级原文证据校验并自动启用 → 简历库汇总（AI 总结预览、最新 AI 评分、原 PDF）→ 条件筛选 → AI 评分、总结与 JD 匹配；React/Vite 工作台通过同域名 Caddy 静态部署，浏览器只请求同源的 `/v1/*` API。

本地启动（开发环境会自动建 SQLite 表并写入院校名单）：

```powershell
python -m pip install -e ".[dev]"
uvicorn app.main:app --reload
```

需要自动 AI 提取时，在另一个终端启动持久 worker。模型凭据只保留在服务端：旧 DeepSeek 兼容路径使用 `DEEPSEEK_API_KEY`，平台 Provider 使用 `RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON` 中与 `credential_ref` 对应的引用：

```powershell
python -m app.ai_extraction_worker
```

前端本地开发（另开一个终端；Vite 会将 `/v1` 和 `/health` 代理到本机 API）：

```powershell
cd web
npm ci
npm run dev
```

云服务器部署说明见 [DEPLOYMENT.md](docs/DEPLOYMENT.md)。生产环境必须先跑 Alembic 迁移和显式院校名单初始化，不能依赖 Web 启动建表；Caddy 是唯一暴露公网端口的服务，同时负责 HTTPS、静态前端和 `/v1/*` API 反代。

## 团队协作、发布与回滚

仓库根目录的 [AGENTS.md](AGENTS.md) 是所有 Codex 和自动化代理的强制项目规则。
每次新任务或新会话开始时，都必须主动检查 GitHub，不需要用户再次提醒。最低开工检查为：

```bash
git status -sb
git fetch origin --prune --tags
git rev-list --left-right --count HEAD...origin/main
git log --oneline HEAD..origin/main
gh pr status
```

只有在本地是干净 `main` 且仅落后远端时，才执行
`git pull --ff-only origin main`。存在未提交修改、分叉分支或重叠 PR 时，先保护现有
改动并使用独立分支/worktree，不得强行拉取、覆盖或把同事修改混入自己的提交。完整异常
处理和交接格式见 [团队共建工作流](docs/TEAM_WORKFLOW.md)。

GitHub 的 `main` 是唯一的团队代码基线；本地开发通过功能分支和 PR 合并到
`main`，生产服务器只部署已经合并并打了 `prod-*` 标签的提交。服务器绝不是
日常开发源，也不能把服务器文件反向覆盖 GitHub。

每一个可验证的步骤都应先提交并推送到 GitHub 分支。完成需求后，先运行后端
测试与前端构建，再创建 PR。PR 合并到 `main` 后，GitHub Actions 会在该提交的 CI
全部通过后自动创建不可变的 `prod-YYYYMMDD-<commit短码>` 标签并部署；详情见
[GitHub Actions CI/CD](docs/CI_CD.md)。本地标签和部署脚本仅用于受控的应急重试。
部署始终只打包 Git 受控源码，不会传输或删除 `.env.production`、数据库、候选人 PDF、
Docker 卷或其他生产数据。

部署脚本会在首次发布或存在 Alembic 迁移时先在服务器项目目录外创建受保护的
PostgreSQL 逻辑备份，随后验证 HTTPS 健康检查、匿名登录保护和受保护 PDF 的拒绝
访问。它在服务器项目外写入不含密钥和候选人资料的发布记录。

回滚只能指向已发布标签，不能以服务器当前文件为来源：

```bash
scripts/rollback-production.sh prod-YYYYMMDD-<commit短码> \
  --ssh-key /path/to/server-key
```

如果目标版本与当前生产版本之间包含迁移，回滚默认会停止。确认旧代码可兼容当前
数据库结构后才可加 `--allow-schema-ahead`；该选项只回滚应用代码，不会自动降级
或恢复数据库。数据库恢复必须由负责人基于部署前备份单独确认。

同事开始工作前执行：

```bash
git pull --ff-only origin main
```

不得直接推送 `main` 或直接修改服务器业务代码。紧急修复也必须回到本地分支、
推送 GitHub、经 PR 合并，并由成功 CI 自动创建新的生产标签。

## 已实现的 API 闭环

- 筛选：`POST /v1/candidates/search`，支持最高学历、精确院校类型（985、211、本科、大专、中专、海外院校）与简称、应届窗口、经历与技能分类、英语证书及分数、奖学金、竞赛获奖、成绩排名、领导经历、泛/精准关键词和保存筛选器；`GET /v1/filter-options` 提供版本化选项。211 严格不包含 985。
- 简历库：`GET /v1/resume-library` 返回每份已上传 PDF 的处理状态、AI 总结预览与最新 AI 评分；不暴露结构化事实明细。
- 原件核验：`GET /v1/resumes/{resume_id}/original-file` 受登录会话保护，以 `inline` PDF 返回；前端使用同源会话的 Blob 预览，不把口令暴露到 URL。
- 评分：创建权重模板（所有维度统一 0 至 100 分制）、按不可变事实快照评分、保留评分历史，并可人工覆写单个维度；支持服务端后台一键批量评分、进度查询、重试和结果复用。
- 总结：生成结构化 AI 总结；事实重存后旧总结会自动标记为历史，不能误作当前总结。
- JD：创建版本化 JD、AI 提取后人工确认需求、按确认版匹配，并返回 JD 条款与简历 fact 证据。

需要模型的接口只读取服务端凭据：旧 DeepSeek 兼容路径读取 `DEEPSEEK_API_KEY`，平台 Provider 路由按 `credential_ref` 从 `RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON` 解析。浏览器、请求体、数据库和审计日志都不传递或保存密钥。接口文档在本地启动后访问 `/docs`。
