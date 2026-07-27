# GitHub Actions CI/CD

## 自托管 Runner

所有工作流都通过标签 `self-hosted`、`Linux`、`X64` 和 `greatsell-ci` 路由到仓库的
受信任 Linux 自托管 Runner，因此不会消耗 GitHub 托管 Actions 分钟。该 Runner 必须保持
在线，并具备 Docker、Docker Compose、Python 3.12、Node 22、Chromium 运行依赖和对 GitHub
Actions 的网络访问；`postgres-mailbox-race`、生产镜像构建和运行时回归会直接使用其 Docker
daemon。

Runner 离线时，工作流会排队而非自动回退到 GitHub 托管 Runner。生产工作流仍仅从受保护的
`main` 或 `prod-*` 进入 `production` Environment，并只在该 Environment 中读取部署密钥与
变量；不要把这些值加入 Runner 配置、仓库变量或源码。

本仓库的自动化分为两层：每个 PR 的完整持续集成（CI），以及在 `main` 发布预检成功后的
自动生产发布（CD）。合并 `main` 本身不会立即连接生产服务器；只有该次 `main` 提交能被
溯源到全绿 PR、完成精确 SHA 镜像构建和无副作用 Compose 预检后，才会自动创建不可变的
`prod-*` 标签并部署。

成功的 `main` CI 会保留已经完成运行时回归的 API 与 Caddy 镜像，并以完整 commit SHA 与 OCI
revision label 标记。**Production release** 在同一受信任 Runner 上校验这两份镜像、通过
受验证的 SSH 通道传输到生产机，再由生产机只做镜像 label 校验、备份、迁移、启动与健康检查；
不会在生产机重新下载 LibreOffice/Tesseract 依赖或重建镜像。这避免把生产服务器的 CPU、磁盘
和网络压力变成发布瓶颈。当前实现依赖 `greatsell-ci` 是同一受信任 Docker Runner；若将来拆分
多个发布 Runner，应改为受控镜像仓库并使用不可变 digest，而不是静默回退到服务器构建。

## 已提供的工作流

- **Continuous integration**：在向 `main` 提交 PR、合并到 `main` 或手动触发时运行。
  PR/手动运行执行 Python 3.12 全量测试、PostgreSQL 邮箱附件去重并发回归、Node 22.12
  前端生产构建、生产镜像构建与完整运行时回归；`main` 运行则只做已绿 PR 溯源、精确镜像
  构建、Compose 校验和该精确镜像的完整运行时回归。
- **Production release**：监听 `main` 上一次成功的 CI `push` 运行。它只接受本仓库
  的 `main` 提交，先用该提交的 Compose 文件和服务器既有 `.env.production` 做只读预检，
  再次确认 `main` 未前进后，创建不可变的 `prod-YYYYMMDD-<commit>` 标签并在同一次工作流中
  部署。发布日志会明确显示镜像传输、不可变源码准备、备份、迁移/启动和健康验证阶段。若预检
  失败，不会创建标签；若预检期间 `main` 前进，它会停止，等待更新提交自己的 CI 成功后再发布。
  保留从 `main` 手动运行并输入 `RELEASE` 的应急入口；该入口同样只接受本地已有、带匹配
  revision label 的 CI 镜像，缺失时会快速失败而不会在服务器上偷偷重建。
  若镜像传输或发布步骤失败，Runner 会暂留该两份已验证镜像，便于从 `main` 使用同一目标提交
  重试；成功或明确跳过发布后才清理它们。
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

1. `main` 必须经 Squash PR 合并，并要求 PR 的完整 CI 与文本完整性检查全部通过。
2. 禁止强制推送和直接推送 `main`。
3. 保护 `prod-*` 标签，禁止移动、删除或复用；仅允许受控发布流程创建标签。

## 日常发布流程

1. PR 合并到 `main`。
2. `main` 的来源证明与发布预检全部变绿。
3. **Production release** 先做服务器配置预检；通过后自动创建标签、部署、验证并保留发布
   记录，无需手动操作。它先传输 CI 已验证的镜像，生产机只加载和校验镜像，随后执行备份、
   迁移、启动和健康验证。`prod-*` 是发布候选，只有服务器的 `current-release.env` 和工作流
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

## CI 分层策略（PR 全量、main 发布预检）

为避免同一变更在 PR 与 `main` 上重复执行耗时的完整回归，自动化按提交阶段分层：

- **PR CI** 是完整的源代码质量门：后端全量 pytest、PostgreSQL 邮箱并发回归、前端构建和 Playwright 关键路径、完整生产镜像运行时回归都在 PR 上执行。
- **main CI** 不重复执行 pytest、PostgreSQL 邮箱并发回归或 Playwright。它先校验当前 `main` 提交确实来自一个已完成全部 PR 检查的合并请求，并校验两者的 Git tree 一致；随后只为该精确 SHA 构建 API/Caddy 镜像、校验 Compose，并对这个将被部署的 API 镜像执行完整 `--all` 运行时回归（包含文档提取及 PostgreSQL 迁移、备份/恢复和 lease recovery）。
- **Production release** 仍只监听成功的 `main` CI。主分支直接推送、缺少全绿 PR、PR 与合并结果代码树不一致，或对应 PR 的文本完整性工作流失败时，镜像构建会在发布前失败，不能触发部署。

`Text encoding integrity` 只在 PR（及手动运行）中执行，避免 `main` 发布预检重复占用 Runner；其成功结果由 `scripts/verify_main_release_provenance.py` 在 `main` 发布预检中校验。

这套设计不依赖私有仓库的 GitHub 分支保护功能：即使有人错误地直接推送 `main`，自动发布也会被代码级溯源门阻止。为保持溯源确定性，团队合并 PR 时应使用 **Squash merge**；溯源门要求合并提交只有一个父提交，并且该父提交就是 PR 检查时的 `base` SHA。因此合并前必须将 PR 更新或 rebase 到最新 `main`，并等待该基线上的完整 PR 检查全部成功；若在检查未结束或基线落后时合并，`main` 发布预检会拒绝，且需要先修正 PR 后重新走合并流程。团队仍应坚持只经 PR 合并，且不应绕过已存在的审核和测试流程。代码级门禁可防止普通误操作，但不能替代未来可用的仓库/组织级写入权限控制。

## Release regression gates

The existing **Backend tests** check runs the complete pytest suite, including
HTML-original delivery safety, trusted-proxy registration throttling, password
recovery expiry/reuse/session invalidation, and tenant-isolation regressions.

The existing **Web build** check additionally installs Chromium and runs the
isolated Playwright critical paths: registration and verification, login,
password recovery, upload, filtering, batch scoring, three-lane JD matching,
mailbox queueing, and cross-workspace denial.

On the trusted self-hosted runner, Playwright's Linux system dependencies are
provisioned once during runner setup. CI downloads Chromium for each checked
out workspace but does not invoke `apt` on every PR.

The existing **Production image builds** check reuses the exact application
image it just built to run `scripts/run_release_regression.py --all`. The
runtime gate exercises real LibreOffice, Tesseract, and document extraction
for all supported formats, then runs temporary PostgreSQL migration,
backup/restore, and worker-lease recovery checks. It creates no production
connection, volume, port, environment file, candidate file, or credential.
