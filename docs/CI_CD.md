# GitHub Actions CI/CD

## 当前镜像交接（TCR）

发布镜像现在由成功的 `main` CI 推送至腾讯云 TCR；Actions artifact 只保存经过校验的小型 metadata，staging 和 production 均按 metadata 中不可变的 `repo@sha256:<manifest>` 直接拉取。它们不会下载或通过 SSH 转发 Docker 镜像归档。TCR 的仓库级变量、Secrets 和排障说明见 [TCR 发布镜像配置](TCR_RELEASE_SETUP.md)。本节优先于本文中任何历史的 archive/镜像传输描述。

## Runner 路由与镜像交接

仓库为私有时，测试工作流通过标签 `self-hosted`、`Linux`、`X64` 和 `greatsell-ci` 路由到
受信任 Linux 自托管 Runner。该 Runner 必须保持在线，并具备 Docker、Docker Compose、Python
3.12、Node 22、Chromium 运行依赖和对 GitHub Actions 的网络访问；`postgres-mailbox-race`、
生产镜像构建和运行时回归会直接使用其 Docker daemon。

仓库为公开时，PR 检查、`main` 溯源与镜像构建、staging 及生产发布编排均自动改用标准
GitHub-hosted Ubuntu Runner。成功的 `main` CI 会把两份带 commit SHA、CI run ID 与 run attempt
标签的镜像，以及 checksum 和 metadata 打包为短期 Actions artifact；**Staging release** 只从触发
它的那次成功 CI 下载该 artifact，校验 checksum、commit、run ID 与 run attempt 后才加载并传输到
staging。镜像不再依赖某台本机 Runner 的
Docker 缓存跨工作流保留。切回私有仓库时，所有这些工作流会自动恢复自托管 Runner，无需改回
YAML；artifact 交接仍保持相同的完整性校验。

这只是工作流的默认路由，不是公开仓库的自托管 Runner 安全边界。公开前必须将该仓库的
repo-level 自托管 Runner 解绑，或将其迁入只允许受保护 `main` 发布工作流的私有部署中继/
组织 Runner group；外部 fork 可以提交修改过的 workflow，不能仅依赖本文件中的 `if` 或
`runs-on` 约束来保护本机。公开仓库使用 GitHub-hosted CI 期间，现有自托管 Runner 的安装、
标签和私有仓库工作流可以保留，转回私有后再重新绑定即可恢复完整链路。

私有仓库的 Runner 离线时，工作流会排队而非自动回退到 GitHub 托管 Runner。生产工作流仍仅从受保护的
`main` 或 `prod-*` 进入 `production` Environment，并只在该 Environment 中读取部署密钥与
变量；不要把这些值加入 Runner 配置、仓库变量或源码。

本仓库的自动化分为三层：每个 PR 的完整持续集成（CI）、成功 `main` 的自动预发布（staging），
以及人工确认的生产晋级（production promotion）。合并 `main` **不会**自动连接生产环境；只有
该次 `main` 提交完成精确 SHA 镜像构建、预发布配置预检、隔离环境部署和公网 smoke 后，才可以
由负责人输入 `PROMOTE` 晋级生产。

成功的 `main` CI 会将已完成运行时回归的 API 与 Caddy 镜像以完整 commit SHA、OCI revision
label、CI run ID 和 run attempt 标记后归档 30 天。**Staging release** 只下载并校验该 CI 的精确 artifact，再使用这些
镜像部署隔离的 staging；它通过后记录 source archive SHA-256、API/Caddy image ID、CI run ID 与健康检查。
**Production promotion** 只接受当前 `main` 的已完成 `stg-*` 候选：它重新下载同一 CI run 的 artifact，复核
checksum、metadata、镜像 label 和 staging 已验收的 image ID，再把该镜像传到目标生产主机，以
`--prebuilt-images` 部署。生产机不重新构建镜像，也不再要求与 staging 位于同一台 Docker 主机。详细操作见
[预发布与生产晋级](STAGING_RELEASE.md)。

## 已提供的工作流

- **Continuous integration**：在向 `main` 提交 PR、合并到 `main` 或手动触发时运行。
  PR/手动运行执行 Python 3.12 全量测试、PostgreSQL 邮箱附件去重并发回归、Node 22.12
  前端生产构建、生产镜像构建与完整运行时回归；`main` 运行则只做已绿 PR 溯源、精确镜像
  构建、Compose 校验和该精确镜像的完整运行时回归。
- **Staging release**：监听本仓库 `main` 上成功的 CI `push` 运行。它只接受当前 main，先用
  该提交的 `deploy/compose.staging.yml` 与服务器既有 `.env.staging` 做只读预检，再创建或复用
  不可变 `stg-YYYYMMDD-<commit>` 标签，传输 CI 已验证镜像、部署隔离 staging 并跑公网 smoke。
  `main` 在预检期间前进时会安全跳过旧候选；staging 成功后 Runner 清理临时镜像，已验收 artifact
  仍保留 30 天，供人工批准后的生产晋级重新下载。
- **Production promotion**：只能从 `main` 手动运行并输入 `PROMOTE`。它先验证当前 main 对应的
  唯一 `stg-*` tag、source archive SHA-256、staging release record、staging 运行中容器与生产
  主机的 API/Caddy image ID；生产目标在校验前会从记录的同一 CI run 下载并验证 artifact，再加载和
  传输镜像。任何不一致都会失败。之后才做生产 `.env.production` 只读预检、创建 `prod-*` 并以同一
  预构建镜像部署。它没有 `workflow_run` 自动生产入口。
- **Production deploy**：只用于手动重部署已有 `prod-*` 标签，必须输入 `DEPLOY`；不再
  监听任意标签推送，避免受保护流程外的标签绕过预检。它同样要求服务器已保有经 staging
  验证的预构建镜像；镜像缺失时快速失败，绝不在生产机重建一个“看起来相同”的镜像。迁移到新的生产
  主机前，当前生产版本及约定的回滚版本必须由受控迁移步骤预加载；不能把 **Production deploy** 当作
  新主机初始化入口。
- **Production bootstrap data import**：只用于新生产主机迁移前的单次数据预灌入，必须从 `main`
  手动输入 `IMPORT_PRODUCTION_SNAPSHOT`。它只消费已经通过私有传输放入 production history
  `incoming/<import-id>` 的固定四文件数据包；重新验证来源、checksum、PostgreSQL dump 与上传归档，
  只恢复两个精确 production 卷。导入没有应用代码、没有 `prod-*` 标签、没有 staging 绕过能力；
  后续仍须走标准 **Production promotion**。详见
  [生产数据导入与首发](PRODUCTION_BOOTSTRAP_IMPORT.md)。
- **Production bootstrap data restore**：仅在上述首个 promotion 失败、无 active release record 时可
  手动输入 `RESTORE_PRODUCTION_BOOTSTRAP`。它恢复保留的已导入数据包并归档相应 pending 标记；
  不属于普通回滚，也不会接触 staging。
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

建议给 `production` 设置 required reviewers；**Production promotion** 会在完成 staging 验证后
暂停等待审批。无论是否启用审批，都应将 Environment 的 deployment branches and tags 限制为
受保护的 `main` 与 `prod-*` 标签，拒绝其他分支；这会阻止从临时分支篡改工作流后申请生产密钥。
服务器上用于部署的 SSH 公钥需要具备非交互式 Docker 权限；生产 `.env.production`
必须仍由服务器本地维护。

同时在 GitHub 分支和标签规则中配置：

1. `main` 必须经已验证 PR 合并，并要求 PR 的完整 CI 与文本完整性检查全部通过。默认使用 Squash merge；普通 GitHub merge commit 仅在其父提交和代码树都严格对应已验证 PR 时受发布门接受。
2. 禁止强制推送和直接推送 `main`。
3. 保护 `prod-*` 标签，禁止移动、删除或复用；仅允许受控发布流程创建标签。

## 日常发布流程

1. PR 合并到 `main`。
2. `main` 的来源证明、镜像构建与运行时回归全部变绿。
3. **Staging release** 自动完成 staging 预检、不可变 `stg-*` 标签、隔离部署和公网 smoke；
   只有 staging 的完成记录、source checksum 与 API/Caddy image ID 均一致时，候选才可晋级。
4. 负责人从 `main` 手动运行 **Production promotion** 并输入 `PROMOTE`。工作流只部署刚才
   验过的同一镜像，创建 `prod-*` 后执行生产备份、迁移、启动和健康验证。
5. 若已创建生产标签的部署因 GitHub runner 中断而未完成，可在 **Production deploy** 中输入同一个
   已创建的标签与确认词 `DEPLOY` 重试；不会创建第二个版本。
6. 如需回滚，在 **Production rollback** 中指定一个已发布的 `prod-*` 标签，并输入
   `ROLLBACK`。数据库与原文件恢复不属于自动回滚范围，必须基于同一 backup ID 的成对
   备份、兼容性评审和显式 `restore-production-backup.sh --confirm-restore` 单独决策。

### 连续合并多个 PR

多个 PR 都显示绿灯时，不能把它们都按同一个旧 `main` 基线连续合并。每合并一个
PR，下一条 PR 必须先执行：

```bash
git fetch origin --prune --tags
git rebase origin/main
git push --force-with-lease
```

然后等待该更新后提交的完整 PR CI 与文本完整性检查重新成功，再进行下一次合并。
这保证发布候选的代码树正是经过验证的代码树；若跳过这一步，`Main release provenance`
会在镜像构建、标签和服务器连接之前失败关闭。

### `Main release provenance` 失败后的纠正流程

这不是可以通过“重跑 main CI”或手动触发 staging 绕过的失败。失败意味着该次合并结果
没有一组与当前 `main` 基线对应的完整 PR 验证证据；在此之前不会构建发布镜像，也不会连接
staging 或 production。

1. 保留失败提交作为审计记录，不重写或直接推送 `main`。
2. 从当前 `origin/main` 新建或更新一个只包含待发布改动的 PR；若原 PR 已被合并，可用一个
   明确、可审查的后续修复 PR 承接，不复制候选人数据、环境文件或部署产物。
3. 等待该 PR 当前 head 的完整 CI 与文本完整性检查全部成功；在准备合并前再次 fetch，确认
   `origin/main` 没有前进。若前进，重新 rebase/update 并重新跑完整 PR CI。
4. 只在上述基线仍一致时合并。新的 `main` CI 会再次验证来源证明，成功后才由
   **Staging release** 自动接力。

该流程故意比“直接重试”多一步：它验证的是新的组合代码树，而不是把上一条 PR 的绿灯错误地
借给已经变化的主分支。

如果历史环境存在无法自动清除的 legacy pending 记录，不能把 `backup=none` 当成无数据，
也不能手工删除该文件。先运行 **Production legacy pending reconciliation**：它会确认 current
记录恰好等于 pending 所记录的前序版本，且数据库和两个持久化卷仍存在，完成新的一组受校验
成对备份后才归档 pending。随后再重新运行 **Production release**。

## 安全与边界

- 公开仓库中，默认 PR、`main` 发布预检和发布编排均必须保持 GitHub-hosted；生产镜像 job 在公开
  仓库只接受 `main` 的 `push`，staging 只能下载同一成功 CI run 的精确 artifact。在公开前还必须解绑 repo-level 自托管 Runner 或使用受限私有
  部署中继，因为外部 fork 可以在自己的分支改写 workflow，YAML 条件本身不是 Runner 隔离。
- CI 不读取生产 Environment 的 SSH 密钥或变量。
- 自动 staging 只接受本仓库的成功 `main` 推送 CI；PR CI、手动 CI、取消或失败的 CI 都不能
  连接 staging。生产只接受已完成 staging 候选的人工 `PROMOTE`。
- 工作流只调用受审阅的 production 脚本（包括预检、发布、回滚和 bootstrap import/restore），并始终显式传入 GitHub Environment 中的目标主机和
  路径，因此不会误用脚本中的历史默认服务器地址。
- 不要把部署密钥、主机指纹、环境文件、候选人 PDF、数据库或任何 API 密钥加入 Git。
- GitHub 使用的 `GITHUB_TOKEN` 创建标签不会触发第二个工作流；因此 **Staging release** 会在
  创建 `stg-*` 后的同一工作流内完成预发布，而 **Production deploy** 仅保留已有生产版本的
  人工重试入口。

详细的服务器发布与恢复行为见 [DEPLOYMENT.md](DEPLOYMENT.md)，团队协作和标签规则见
[TEAM_WORKFLOW.md](TEAM_WORKFLOW.md)。

## CI 分层策略（PR 全量、main 发布预检）

为避免同一变更在 PR 与 `main` 上重复执行耗时的完整回归，自动化按提交阶段分层：

- **PR CI** 是完整的源代码质量门：后端全量 pytest、PostgreSQL 邮箱并发回归、前端构建和 Playwright 关键路径、完整生产镜像运行时回归都在 PR 上执行。
- **main CI** 不重复执行 pytest、PostgreSQL 邮箱并发回归或 Playwright。它先校验当前 `main` 提交确实来自一个已完成全部 PR 检查的合并请求，并校验两者的 Git tree 一致；随后只为该精确 SHA 构建 API/Caddy 镜像、校验 Compose，并对这个将被部署的 API 镜像执行完整 `--all` 运行时回归（包含文档提取及 PostgreSQL 迁移、备份/恢复和 lease recovery）。
- **Staging release** 只监听成功的 `main` CI。主分支直接推送、缺少全绿 PR、PR 与合并结果代码树不一致，或对应 PR 的文本完整性工作流失败时，镜像构建会在预发布前失败，不能触发部署；生产还需要人工 `PROMOTE` 和完整 staging attestation。

`Text encoding integrity` 只在 PR（及手动运行）中执行，避免 `main` 发布预检重复占用 Runner；其成功结果由 `scripts/verify_main_release_provenance.py` 在 `main` 发布预检中校验。

这套设计不依赖私有仓库的 GitHub 分支保护功能：即使有人错误地直接推送 `main`，自动发布也会被代码级溯源门阻止。为保持溯源确定性，团队默认使用 **Squash merge**；溯源门也接受 GitHub 普通 merge commit，但只能是严格的 `parents = [PR base SHA, PR head SHA]`，且最终 Git tree 必须与已验证 PR head 完全一致。普通 merge 因基线落后或冲突处理导致代码树变化时仍会失败关闭；多提交的 GitHub rebase merge 也不在当前门禁支持范围内。无论采用哪种允许的合并方式，合并前都必须将 PR 更新或 rebase 到最新 `main`，并等待该基线上的完整 PR 检查全部成功；若在检查未结束或基线落后时合并，`main` 发布预检会拒绝，且需要先修正 PR 后重新走合并流程。团队仍应坚持只经 PR 合并，且不应绕过已存在的审核和测试流程。代码级门禁可防止普通误操作，但不能替代未来可用的仓库/组织级写入权限控制。

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
runtime gate exercises real LibreOffice and document extraction for all
supported formats, verifies the Tencent image-OCR request contract without a
paid cloud credential, and confirms Tesseract is absent from the production
image. It then runs temporary PostgreSQL migration,
backup/restore, and worker-lease recovery checks. It creates no production
connection, volume, port, environment file, candidate file, or credential.
