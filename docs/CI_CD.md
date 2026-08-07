# GitHub Actions CI/CD

## 镜像交接

`main` CI 不再构建或推送发布镜像。staging 的镜像由 GitHub-hosted 发布 Runner 直接构建该
commit 的 API/Caddy 镜像，校验 `org.opencontainers.image.revision` label 与 image ID 后经 SSH
流式传输到 staging 主机（`docker save | gzip | ssh docker load`），全程不过境、不碰中国 TCR。

生产镜像来源是**国内构建 + 人工发**：**Production promotion**（`PROMOTE` + production
Environment 审批）手动触发时，`deploy-production.sh` 把已验收 `prod-*` 标签的源码归档流式传输到
生产机，并在生产机上以 Dockerfile 内置的腾讯镜像 build args（`DEBIAN_MIRROR` /
`PIP_INDEX_URL` / `NPM_REGISTRY`）执行 `docker compose build api caddy`，随后备份、迁移、启动
与健康验证。生产机位于国内，基础镜像与包拉取都走腾讯镜像，避免跨境吞吐瓶颈；镜像不跨
`docker save/load`，不占用 staging，也不依赖发布 Runner 的构建产物。生产机地址与 SSH key 不进
仓库：生产部署密钥只放在 `production` Environment，由人工 promotion 使用。历史 TCR 方案
（仓库级变量、Secrets、排障说明）见 [TCR 发布镜像配置](TCR_RELEASE_SETUP.md)，仅作参考。
本节优先于本文中任何历史的 archive/镜像传输描述。

## Runner 路由与镜像交接

测试工作流（PR 检查、`main` 溯源、Playwright 关键路径、PostgreSQL 并发回归）在仓库为私有时
通过标签 `self-hosted`、`Linux`、`X64` 和 `greatsell-ci` 路由到受信任 Linux 自托管 Runner；
仓库为公开时自动改用标准 GitHub-hosted Ubuntu Runner。自托管 Runner 必须保持在线，并具备
Docker、Docker Compose、Python 3.12、Node 22、Chromium 运行依赖和对 GitHub Actions 的
网络访问；`postgres-mailbox-race` 和 Playwright 会直接使用其 Docker daemon。

历史上 `production-images` job 在 GitHub-hosted Runner 上构建并推送到中国 TCR，再让 staging 和
生产各自拉取。该 job 已随本迁移移除：`main` CI 不再跨境构建或推送镜像。staging 的镜像构建
仍然只在 GitHub-hosted 发布 Runner 上进行，不依赖自托管 Runner；`docker build` 的腾讯镜像
build args 让基础镜像、apt 与包索引拉取保持快速。自托管 Runner 即使离线或卡死也不会阻塞
staging 发布。

仓库为公开时，PR 检查、`main` 溯源、staging 发布编排均使用标准 GitHub-hosted Ubuntu Runner。
`main` CI 在合并后只执行 `main-release-provenance` 校验（验证该 commit 确实来自全绿 PR），
不再构建或推送任何镜像。staging 在发布 Runner 上用腾讯镜像 build args 直接构建该 commit 的
API/Caddy 镜像，校验 revision label 与 image ID 后经 SSH 流式传输到 staging 主机，部署成功后
立即删除 staging 上旧的 app 镜像，只保留新 SHA 一套。镜像不再依赖某台本机 Runner 的 Docker
缓存跨工作流保留。

这只是工作流的默认路由，不是公开仓库的自托管 Runner 安全边界。公开前必须将该仓库的
repo-level 自托管 Runner 解绑，或将其迁入只允许受保护 `main` 发布工作流的私有部署中继/
组织 Runner group；外部 fork 可以提交修改过的 workflow，不能仅依赖本文件中的 `if` 或
`runs-on` 约束来保护本机。公开仓库使用 GitHub-hosted CI 期间，现有自托管 Runner 的安装、
标签和私有仓库测试工作流可以保留，转回私有后再重新绑定即可恢复测试链路；发布镜像构建
（staging）始终使用 GitHub-hosted。

私有仓库的 Runner 离线时，测试工作流会排队而非自动回退到 GitHub 托管 Runner；staging 发布
仍走 GitHub-hosted 发布 Runner，不受影响。生产工作流仍仅从受保护的
`main` 或 `prod-*` 进入 `production` Environment，并只在该 Environment 中读取部署密钥与
变量；不要把这些值加入 Runner 配置、仓库变量或源码。

本仓库的自动化分为两层：每个 PR 的完整持续集成（CI）和成功 `main` 的自动预发布（staging）。
合并 `main` **不会**自动连接生产环境。生产晋级链（production promotion）采用"国内构建 + 人工发"：
负责人手动运行 **Production promotion**（`PROMOTE` + production Environment 审批）时，才在
生产机上构建并发布该候选。

`main` CI 在合并后只做 `main-release-provenance` 校验。**Staging release** 在发布 Runner 上
构建该 commit 的 API/Caddy 镜像，直接流式传输到隔离 staging，校验 revision label 与 image ID
后部署并跑公网 smoke；通过后记录 source archive SHA-256、API/Caddy image ID、
`image_delivery=direct` 与健康检查。**Production promotion** 只接受当前 `main` 的已完成
`stg-*` 候选：校验 staging 完成记录与源码 archive SHA-256 一致后，把该候选源码在生产机上以
腾讯镜像参数构建并部署，创建 `prod-*` 标签记录这次发布。生产构建与发布只在人工
`PROMOTE` + production Environment 审批后发生。详细操作见
[预发布与生产晋级](STAGING_RELEASE.md)。

## 已提供的工作流

- **Continuous integration**：在向 `main` 提交 PR、合并到 `main` 或手动触发时运行。
  PR/手动运行执行 Python 3.12 全量测试、PostgreSQL 邮箱附件去重并发回归、Node 22.12
  前端生产构建和 Playwright 关键路径；`main` 运行只做已绿 PR 溯源（`main-release-provenance`），
  不再构建或推送镜像。
- **Staging release**：监听本仓库 `main` 上成功的 CI `push` 运行。它只接受当前 main，先用
  该提交的 `deploy/compose.staging.yml` 与服务器既有 `.env.staging` 做只读预检，再创建或复用
  不可变 `stg-YYYYMMDD-N` 标签（日期 + 当日序号，过渡期兼容历史 `<commit>` 后缀格式），在
  美国发布 Runner 上构建该 commit 的 API/Caddy 镜像、经 SSH 直接流式传输到 staging 主机
  （不经中国 TCR），部署隔离 staging 并跑公网 smoke，然后删除 staging 上旧的 app 镜像，只保留
  新 SHA 一套。它不持有任何生产凭据，也不连接生产。`main` 在预检期间前进时会安全跳过旧候选。
- **Production promotion**：只能从 `main` 手动运行并输入 `PROMOTE`。它先验证当前 main 对应的
  唯一 `stg-*` tag、source archive SHA-256、staging release record（direct 记录核对 API/Caddy
  image ID 与 revision）均一致，再创建不可变 `prod-YYYYMMDD-N` 标签，并把该标签源码归档流式
  传输到生产机，在生产机上以腾讯镜像参数执行 `compose build api caddy` 后备份、迁移、启动与
  健康验证。生产构建与发布只在 `production` Environment 审批后发生；构建/发布失败会失败关闭，
  可重跑 **Production deploy**（复用已有 `prod-*` 标签）。
- **Production deploy**：只用于手动重部署已有 `prod-*` 标签，必须输入 `DEPLOY`；不再
  监听任意标签推送，避免受保护流程外的标签绕过预检。它与 promotion 一样在生产机上以腾讯镜像
  参数从该标签源码构建并部署，是已有版本的重试/恢复入口，不是新代码发布入口。
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
2. `main` 的来源证明（`main-release-provenance`）变绿，随后 **Staging release** 自动完成 staging
   预检、不可变 `stg-*` 标签、隔离部署和公网 smoke；只有 staging 的完成记录、source checksum 与
   API/Caddy image ID 均一致时，候选才可晋级。
3. 负责人从 `main` 手动运行 **Production promotion** 并输入 `PROMOTE`。工作流先校验 staging
   完成记录（direct 记录核对 API/Caddy image ID 与 revision）与源码 archive SHA-256 一致，
   再创建 `prod-YYYYMMDD-N`，在生产机上以腾讯镜像参数构建 API/Caddy 镜像并执行生产备份、
   迁移、启动和健康验证。
4. 若已创建生产标签的部署因 GitHub runner 中断而未完成，可在 **Production deploy** 中输入同一个
   已创建的标签与确认词 `DEPLOY` 重试；不会创建第二个版本。
5. 如需回滚，在 **Production rollback** 中指定一个已发布的 `prod-*` 标签，并输入
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
会在 staging 构建镜像和部署之前失败关闭。

### `Main release provenance` 失败后的纠正流程

这不是可以通过“重跑 main CI”或手动触发 staging 绕过的失败。失败意味着该次合并结果
没有一组与当前 `main` 基线对应的完整 PR 验证证据；在此之前不会构建 staging 镜像，也不会连接
staging。

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

- 公开仓库中，默认 PR、`main` 发布预检和发布编排均必须保持 GitHub-hosted；`main` CI 不再构建或推送镜像，staging 直接构建并流式传输镜像（不经 TCR）。在公开前还必须解绑 repo-level 自托管 Runner 或使用受限私有
  部署中继，因为外部 fork 可以在自己的分支改写 workflow，YAML 条件本身不是 Runner 隔离。
- CI 不读取生产 Environment 的 SSH 密钥或变量。
- 自动 staging 只接受本仓库的成功 `main` 推送 CI；PR CI、手动 CI、取消或失败的 CI 都不能
  连接 staging。生产只接受已完成 staging 候选的人工 `PROMOTE`。
- 生产镜像在生产机上从标签源码以腾讯镜像参数构建；构建与发布只在 `production` Environment
  审批后发生，自动 staging 不连接生产、不持有任何生产凭据。
- 工作流只调用受审阅的 production 脚本（包括预检、发布、回滚和 bootstrap import/restore），并始终显式传入 GitHub Environment 中的目标主机和
  路径，因此不会误用脚本中的历史默认服务器地址。
- 不要把部署密钥、主机指纹、环境文件、候选人 PDF、数据库或任何 API 密钥加入 Git。
- GitHub 使用的 `GITHUB_TOKEN` 创建标签不会触发第二个工作流；因此 **Staging release** 会在
  创建 `stg-*` 后的同一工作流内完成预发布，而 **Production deploy** 仅保留已有生产版本的
  人工重试入口。

详细的服务器发布与恢复行为见 [DEPLOYMENT.md](DEPLOYMENT.md)，团队协作和标签规则见
[TEAM_WORKFLOW.md](TEAM_WORKFLOW.md)。

## CI 分层策略（PR 全量、main 溯源）

为避免同一变更在 PR 与 `main` 上重复执行耗时的完整回归，自动化按提交阶段分层：

- **PR CI** 是完整的源代码质量门：后端全量 pytest、PostgreSQL 邮箱并发回归、前端构建和
  Playwright 关键路径都在 PR 上执行。镜像构建不在 PR 上重复执行。
- **main CI** 不重复执行 pytest、PostgreSQL 邮箱并发回归或 Playwright。它只校验当前 `main`
  提交确实来自一个已完成全部 PR 检查的合并请求，并校验两者的 Git tree 一致；不再构建或推送
  镜像。镜像由 **Staging release** 在发布 Runner 上按精确 SHA 构建。
- **Staging release** 只监听成功的 `main` CI。主分支直接推送、缺少全绿 PR、PR 与合并结果
  代码树不一致，或对应 PR 的文本完整性工作流失败时，`main-release-provenance` 会失败关闭，
  staging 不会构建或部署。

`Text encoding integrity` 只在 PR（及手动运行）中执行，避免 `main` 溯源重复占用 Runner；其成功
结果由 `scripts/verify_main_release_provenance.py` 在 `main` 溯源中校验。

这套设计不依赖私有仓库的 GitHub 分支保护功能：即使有人错误地直接推送 `main`，staging 也会被
代码级溯源门阻止。为保持溯源确定性，团队默认使用 **Squash merge**；溯源门也接受 GitHub 普通
merge commit，但只能是严格的 `parents = [PR base SHA, PR head SHA]`，且最终 Git tree 必须与
已验证 PR head 完全一致。普通 merge 因基线落后或冲突处理导致代码树变化时仍会失败关闭；多提交的
GitHub rebase merge 也不在当前门禁支持范围内。无论采用哪种允许的合并方式，合并前都必须将 PR
更新或 rebase 到最新 `main`，并等待该基线上的完整 PR 检查全部成功；若在检查未结束或基线落后时
合并，`main` 溯源会拒绝，且需要先修正 PR 后重新走合并流程。团队仍应坚持只经 PR 合并，且不应绕过
已存在的审核和测试流程。代码级门禁可防止普通误操作，但不能替代未来可用的仓库/组织级写入权限控制。

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

The release runtime regression harness (`scripts/run_release_regression.py --all`)
exercises real LibreOffice and document extraction for all supported formats,
verifies the Tencent image-OCR request contract without a paid cloud credential,
confirms Tesseract is absent from the production image, and runs temporary
PostgreSQL migration, backup/restore, and worker-lease recovery checks. It is
no longer wired into main CI (the image-building job was removed); it is kept
as the runtime gate for the future production image path and can be run against
a locally built image. It creates no production connection, volume, port,
environment file, candidate file, or credential.
