# 发布运行时回归执行手册

这套 harness 只使用运行时生成的合成数据。它不会读取 `.env.production`、不会启动
`compose.yml`、不会映射宿主机端口、不会使用既有 Docker 卷，也不会连接服务器。文档提取
容器显式使用 Docker `--network none`；恢复演练的应用与 PostgreSQL 只加入 Docker
`--internal` 临时网络，不能访问外网。

## 前置条件

- Docker daemon 可用；
- 本地可执行 Python 3.12+；
- 首次执行允许 Docker 拉取 `postgres:16-alpine`，并构建项目 `Dockerfile`。

运行时会生成随机数据库口令，但该值只存在于子进程环境中，不会写入仓库、日志、备份或
输出。正常退出或可捕获的异常时，所有容器、网络、临时 PostgreSQL 数据、临时上传文件和
脚本构建的临时镜像都会自动删除。

每次运行还会给其 Docker 资源打上以下标签，便于主机被强制关机、Docker daemon 崩溃等无法
执行 `finally` 的情况后进行精确清理：

- `com.greatsell.release-regression=true`
- `com.greatsell.release-regression.run=<本次随机运行 ID>`

PowerShell 清理异常遗留资源：

```powershell
$filter = 'label=com.greatsell.release-regression=true'
docker ps -aq --filter $filter | ForEach-Object { docker rm -f $_ }
docker network ls -q --filter $filter | ForEach-Object { docker network rm $_ }
docker volume ls -q --filter $filter | ForEach-Object { docker volume rm $_ }
docker image ls -q --filter $filter | Sort-Object -Unique | ForEach-Object { docker image rm $_ }
```

Linux / CI 可使用：

```bash
filter='label=com.greatsell.release-regression=true'
docker ps -aq --filter "$filter" | xargs -r docker rm -f
docker network ls -q --filter "$filter" | xargs -r docker network rm
docker volume ls -q --filter "$filter" | xargs -r docker volume rm
docker image ls -q --filter "$filter" | sort -u | xargs -r docker image rm
```

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
3. 创建带运行标签的真实 Docker uploads 卷，用真实 `pg_dump -Fc` 与只读卷挂载生成同一 ID 的 `database.dump`、`uploads.tar.gz`、清单和 SHA-256；
4. 删除原数据库容器和原 uploads 卷后，恢复到全新的 PostgreSQL 与全新的命名卷，并以只读卷挂载校验 Alembic head、工作区归属、原文件 SHA-256 与业务记录；
5. 让主工作区各一条 `running + expired lease` 邮箱后台任务与 AI 提取任务分别通过其真实 worker recovery/claim 函数回到队列并被重新领取，同时断言第二工作区对应两条任务都没有被触碰。

这不是生产备份替代品：生产备份、保留期限、异地副本和恢复授权仍由部署负责人按
`docs/DEPLOYMENT.md` 执行。本演练只是将应用迁移、逻辑数据库备份、原文件备份和后台队列
恢复的核心兼容性变成可重复的发布门槛。
