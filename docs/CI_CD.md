# GitHub Actions CI/CD

本仓库的自动化分为两层：每个 PR 的持续集成（CI），以及在 `main` CI 成功后的自动
生产发布（CD）。合并 `main` 本身不会立即连接生产服务器；只有该次 `main` 提交的 CI
四项检查全部成功后，才会先对服务器既有环境执行无副作用 Compose 预检，通过后自动创建
不可变的 `prod-*` 标签并部署。

## 已提供的工作流

- **Continuous integration**：在向 `main` 提交 PR、合并到 `main` 或手动触发时运行。
  它分别执行 Python 3.12 全量测试、PostgreSQL 邮箱附件去重并发回归、Node 22.12
  前端生产构建，以及应用与 Caddy 两个生产镜像构建和 Compose 配置校验。
- **Production release**：监听 `main` 上一次成功的 CI `push` 运行。它只接受本仓库
  的 `main` 提交，先用该提交的 Compose 文件和服务器既有 `.env.production` 做只读预检，
  再次确认 `main` 未前进后，创建不可变的 `prod-YYYYMMDD-<commit>` 标签并在同一次工作流中
  部署。若预检失败，不会创建标签；若预检期间 `main` 前进，它会停止，等待更新提交自己的
  CI 成功后再发布。保留从 `main` 手动运行并输入 `RELEASE` 的应急入口。
- **Production deploy**：只用于手动重部署已有 `prod-*` 标签，必须输入 `DEPLOY`；不再
  监听任意标签推送，避免受保护流程外的标签绕过预检。
- **Production rollback**：只能手动触发，且只接受已有 `prod-*` 标签。默认拒绝
  回滚到数据库 schema 落后的代码；只有已确认兼容时才可显式允许该情形。
- **Production legacy pending reconciliation**：仅用于历史发布器遗留的中断
  `pending-release.env`。它必须由管理员从 `main` 手动输入记录中的精确 tag、40 位 commit
  和确认词；在所有应用写入服务已经停止时，先创建并校验新的 PostgreSQL + uploads 成对
  备份，再归档 pending 记录。该流程还会校验数据库与 API/worker 容器实际挂载的是受 Compose
  管理的两个持久化卷（兼容历史无标签 uploads 卷时要求 API/worker 双重挂载证明），并记录
  已退出迁移容器的状态；运行中的迁移容器会被拒绝。它不部署、
  构建、迁移、重启服务或读取 `.env.production`。

发布与回滚仍复用仓库已有的脚本。它们会在每次变更前创建统一 backup ID 的 PostgreSQL
逻辑备份与 `uploads_data` 原件卷备份，校验 checksum 后再执行发布，并保留发布记录、健康
检查、登录保护与原文件鉴权检查；不会传输、覆盖或删除 `.env.production`、数据库、候选人
文件、Docker 卷、Caddy 配置或 DNS。

## 一次性 GitHub 配置

在仓库 **Settings → Environments** 创建名为 `production` 的 Environment，并将以下
值都配置在该 Environment 内，而不是提交进仓库或放到普通 Repository secrets：

### Secrets

- `PROD_SSH_PRIVATE_KEY`：只拥有部署权限的 SSH 私钥。
- `PROD_SSH_KNOWN_HOSTS`：已通过可信渠道核验过的生产主机 `known_hosts` 条目。
  工作流会校验该条目存在，缺失时立即失败；不要在工作流中临时使用不受验证的
  `ssh-keyscan` 结果。

### Variables

- `PROD_DEPLOY_HOST`：部署用户名与主机，例如 `ubuntu@<production-host>`。
- `PROD_PROJECT_DIR`：服务器已有项目目录，例如
  `/home/ubuntu/resume-screening-v3`。
- `PROD_HISTORY_DIR`：项目目录之外、供发布记录和逻辑备份使用的目录。它必须沿用
  当前人工发布所使用的受保护目录。

要启用**无人值守自动发布**，不要给 `production` 设置 required reviewers。若未来需要
人工闸门，可以开启审批规则；此时自动发布会在部署前暂停等待审批，而不会跳过 CI。
无论是否启用审批，都应将 Environment 的 deployment branches and tags 限制为受保护的
`main` 与 `prod-*` 标签，拒绝其他分支；这会阻止从临时分支篡改工作流后申请生产密钥。
服务器上用于部署的 SSH 公钥需要具备非交互式 Docker 权限；生产 `.env.production`
必须仍由服务器本地维护。

同时在 GitHub 分支和标签规则中配置：

1. `main` 必须经 PR 合并，并要求四项 CI 检查全部通过。
2. 禁止强制推送和直接推送 `main`。
3. 保护 `prod-*` 标签，禁止移动、删除或复用；仅允许受控发布流程创建标签。

## 日常发布流程

1. PR 合并到 `main`。
2. `main` CI 的四项检查全部变绿。
3. **Production release** 先做服务器配置预检；通过后自动创建标签、部署、验证并保留发布
   记录，无需手动操作。`prod-*` 是发布候选，只有服务器的 `current-release.env` 和工作流
   成功才表示已部署。
4. 若部署因 GitHub runner 中断而未完成，可在 **Production deploy** 中输入同一个
   已创建的标签与确认词 `DEPLOY` 重试；不会创建第二个版本。
5. 如需回滚，在 **Production rollback** 中指定一个已发布的 `prod-*` 标签，并输入
   `ROLLBACK`。数据库与原文件恢复不属于自动回滚范围，必须基于同一 backup ID 的成对
   备份、兼容性评审和显式 `restore-production-backup.sh --confirm-restore` 单独决策。

如果历史环境存在无法自动清除的 legacy pending 记录，不能把 `backup=none` 当成无数据，
也不能手工删除该文件。先运行 **Production legacy pending reconciliation**：它会确认 current
记录恰好等于 pending 所记录的前序版本，且数据库和两个持久化卷仍存在，完成新的一组受校验
成对备份后才归档 pending。随后再重新运行 **Production release**。

## 安全与边界

- CI 不读取生产 Environment 的 SSH 密钥或变量。
- 自动发布只接受本仓库的 `main` 推送 CI；PR CI、手动 CI、取消或失败的 CI 都不能发布。
- 工作流只调用现有 `scripts/preflight-production-release.sh`、`scripts/deploy-production.sh` 与
  `scripts/rollback-production.sh`，并始终显式传入 GitHub Environment 中的目标主机和
  路径，因此不会误用脚本中的历史默认服务器地址。
- 不要把部署密钥、主机指纹、环境文件、候选人 PDF、数据库或任何 API 密钥加入 Git。
- GitHub 使用的 `GITHUB_TOKEN` 创建标签不会触发第二个工作流；因此 **Production release**
  会在创建标签后的同一工作流内完成部署，而 **Production deploy** 仅保留人工重试入口。

详细的服务器发布与恢复行为见 [DEPLOYMENT.md](DEPLOYMENT.md)，团队协作和标签规则见
[TEAM_WORKFLOW.md](TEAM_WORKFLOW.md)。

## Release regression gates

The existing **Backend tests** check runs the complete pytest suite, including
HTML-original delivery safety, trusted-proxy registration throttling, password
recovery expiry/reuse/session invalidation, and tenant-isolation regressions.

The existing **Web build** check additionally installs Chromium and runs the
isolated Playwright critical paths: registration and verification, login,
password recovery, upload, filtering, batch scoring, three-lane JD matching,
mailbox queueing, and cross-workspace denial.

The existing **Production image builds** check reuses the exact application
image it just built to run `scripts/run_release_regression.py --all`. The
runtime gate exercises real LibreOffice, Tesseract, and document extraction
for all supported formats, then runs temporary PostgreSQL migration,
backup/restore, and worker-lease recovery checks. It creates no production
connection, volume, port, environment file, candidate file, or credential.
