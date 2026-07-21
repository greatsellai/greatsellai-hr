# 发布运行时回归执行手册

这套 harness 只使用运行时生成的合成数据。它不会读取 `.env.production`、不会启动
`compose.yml`、不会映射宿主机端口、不会使用既有 Docker 卷，也不会连接服务器。

## 前置条件

- Docker daemon 可用；
- 本地可执行 Python 3.12+；
- 首次执行允许 Docker 拉取 `postgres:16-alpine`，并构建项目 `Dockerfile`。

运行时会生成随机数据库口令，但该值只存在于子进程环境中，不会写入仓库、日志、备份或
输出。所有容器、网络、临时 PostgreSQL 数据和临时上传文件都会在结束时删除。

## 本地 PowerShell

在仓库根目录执行：

```powershell
.\scripts\run-release-regression.ps1
```

只验证 Docker 镜像内的多格式提取：

```powershell
.\scripts\run-release-regression.ps1 -Documents
```

只验证临时 PostgreSQL 的迁移、备份恢复和 Worker 租约恢复：

```powershell
.\scripts\run-release-regression.ps1 -Postgres
```

## CI / Linux

Python 驱动不依赖 PowerShell，适合直接作为 CI step：

```bash
python scripts/run_release_regression.py --all
```

如果当前 job 已经构建了应用镜像，可避免重复构建：

```bash
python scripts/run_release_regression.py --all --image greatsellai-hr-ci:local
```

镜像必须由仓库根目录的 `Dockerfile` 构建，且包含当前提交的 `app/`、`migrations/` 与
`pyproject.toml`。脚本会把受版本控制的运行时 runner 以只读 bind mount 注入该镜像；这
使测试代码不会被误打入生产镜像，同时仍会在生产依赖和运行用户下执行。

## 覆盖内容

`--documents` 会在项目 Docker 镜像内动态生成并提取：

- PDF；
- DOCX（真实 `soffice --headless --convert-to pdf`）；
- XLSX（真实 OpenPyXL 读取）；
- PNG / JPG（真实 `tesseract -l chi_sim+eng`）；
- HTML（真实 BeautifulSoup 脚本清理后提取）。

每类 fixture 都有无隐私标记，harness 会检查解析器标识、页计数和标记文本。它不会 mock
LibreOffice、Tesseract 或项目的统一 `extract_document_text` 链路。

`--postgres` 会：

1. 在临时 PostgreSQL 中先升级至最早的 Alembic revision，再升级到当前唯一 head；
2. 写入两个合成工作区、一个简历元数据记录、一个原始文件和两个隔离的邮箱 worker 任务；
3. 用真实 `pg_dump -Fc` 备份数据库，并用 tar.gz 备份 uploads；
4. 恢复到新的临时 PostgreSQL 和新的 uploads 目录，校验 Alembic head、工作区归属、原文件 SHA-256 与业务记录；
5. 让主工作区的 `running + expired lease` 邮箱任务通过真实 worker recovery/claim 函数回到队列并被重新领取，同时断言第二工作区任务没有被触碰。

这不是生产备份替代品：生产备份、保留期限、异地副本和恢复授权仍由部署负责人按
`docs/DEPLOYMENT.md` 执行。本演练只是将应用迁移、逻辑数据库备份、原文件备份和后台队列
恢复的核心兼容性变成可重复的发布门槛。
