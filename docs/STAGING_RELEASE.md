# 预发布与生产晋级

> 当前流程：staging 在美国发布 Runner 上直接构建该 commit 的镜像并经 SSH 流式传输到 staging 主机
> （不经中国 TCR），部署成功后只保留新 SHA 一套镜像。生产采用"国内构建 + 人工发"：负责人手动
> 运行 **Production promotion**（`PROMOTE` + production Environment 审批）时，才在生产机上以
> Dockerfile 内置的腾讯镜像参数构建该候选的 API/Caddy 镜像并发布。生产机地址与 SSH key 不进
> 仓库：生产部署密钥只放在 `production` Environment。
> 版本号使用日期 + 当日序号（`stg-YYYYMMDD-N` / `prod-YYYYMMDD-N`，过渡期兼容历史
> `<commit>` 后缀标签）。历史 TCR 方案见 [TCR 发布镜像配置](TCR_RELEASE_SETUP.md)，仅作参考。

## 发布链路

```text
PR 完整 CI
  → 合并 main
  → main CI 验证来源（main-release-provenance）
  → 自动构建 API / Caddy 镜像并流式传输到 staging，部署并执行公网 smoke
  → 人工运行 Production promotion（PROMOTE + production Environment 审批）
  → 校验 staging 记录 + 源码 archive SHA-256 → 创建 prod-YYYYMMDD-N → 在生产机构建并部署
```

生产不会因为 `main` 合并自动发布。生产构建与发布只在 `production` Environment 审批后发生；
staging-release job 不持有任何生产凭据，也不连接生产。预发布未完成、冒烟检查失败、`main` 在
验收期间前进时 staging 会失败关闭；生产构建或发布失败时 promotion 也会失败关闭，不会误发。

当前预发布和生产可以同机，也可以部署在不同 Docker 主机；它们始终是两个独立 Compose 项目。
生产晋级不依赖两端共享 Docker 镜像缓存：生产镜像直接在生产机上从 `prod-*` 标签源码构建，staging
的镜像产物不跨主机传输。生产构建以
`RESUME_V3_ENVIRONMENT=production` 运行，以覆盖生产专属的 HTTPS、安全、数据生命周期和投递逻辑；隔离由独立项目、数据库、数据卷、网络、代理地址与预发布域名保证：

- production：`resume-screening-v3`、生产 `.env.production`、生产数据卷和 `172.30.0.0/24`；
- staging：`resume-screening-v3-staging`、独立 `.env.staging`、独立数据卷和 `172.31.0.0/24`；
- staging Caddy 仍监听旧主机私有 `172.17.0.1:18080`。在生产迁移到新主机期间，旧主机上
  **已经运行的** Caddy 保留 `staging.hr.greatsellai.net` 的既有精确路由；它不属于新生产
  Compose 项目，也不会被任何生产发布、回滚或恢复工作流刷新、重建或接管。

预发布不携带生产数据库、PDF 或任何生产数据快照。`.env.staging` 必须逐项人工复制当前
生产实际运行值（包含非密钥开关、模型、限额和超时），但它仍是独立文件，绝不能直接引用
`.env.production`；仅数据库连接、文件卷、网络、Trusted Proxy、公开 URL 和 OAuth 回调保持
staging 值。生产数据快照是另一个显式、单向、脱敏优先的运维任务，绝不能被部署工作流自动执行。

同一 Google 或 Microsoft OAuth Client 还必须在供应商控制台额外允许 staging 的两个回调地址，
否则会出现 `redirect_uri_mismatch`。

## GitHub Environments

当前迁移桥接期不运行任何 “staging gateway bootstrap”。旧机 Caddy 是保留中的既有运行时，
不是新生产环境的一部分；不要停止、重建或用新版本覆盖它。日常流程仍是 CI 自动部署
staging，最后由人工 `PROMOTE` 晋级生产；这些流程均不会修改旧机的 staging 网关。

在 **Settings → Environments** 创建 `staging`。所有值只放在该 Environment，不进入仓库、
Runner 常驻环境或代码。

### `staging` secrets

- `STAGING_SSH_PRIVATE_KEY`
- `STAGING_SSH_KNOWN_HOSTS`

### `staging` variables

- `STAGING_DEPLOY_HOST`：例如 `ubuntu@<same-host>`；
- `STAGING_PROJECT_DIR`：`/home/ubuntu/resume-screening-v3-staging`；
- `STAGING_HISTORY_DIR`：`/home/ubuntu/greatsellai-hr-staging-deployments`；
- `STAGING_PUBLIC_URL`：`https://staging.hr.greatsellai.net`。

`staging` Environment 只放 staging 自身的凭据，绝不包含任何生产密钥；自动 staging-release job
因此无法连接生产。`production` 的 `PROD_*` secrets/variables 必须只指向新生产主机；`staging` 的
`STAGING_*` 继续指向旧主机。建议为 `production` 配置 Required reviewer；这会让
**Production promotion** 在验证完 staging 后暂停，直到负责人批准。

`STAGING_DEPLOY_HOST` 与 `PROD_DEPLOY_HOST` 可以指向不同 Docker 主机。生产晋级只接受 staging
记录里的已完成候选：promotion 校验 staging release record（direct 记录核对 API/Caddy image ID
与 revision）与源码 archive SHA-256 一致后，创建 `prod-*`，并在生产机上从该标签源码以腾讯镜像
参数构建 API/Caddy 镜像再部署。`PROD_DEPLOY_HOST` 只用于 promotion 时连生产机做校验、构建与
发布。

## 服务器一次性准备

1. 创建 `/home/ubuntu/resume-screening-v3-staging`，只放置 `.env.staging` 和由发布流程写入的
   `compose.yml`；不要创建 `.env.production`。
2. 从 `.env.staging.example` 创建独立 `.env.staging`：只为 staging 数据库生成专用密码；其余
   运行时服务值必须逐项复制当前生产实际值，但必须保留 staging 的 Trusted Proxy、公开 URL 和 OAuth
   回调地址，且不得直接引用或挂载 `.env.production`。
3. 创建 staging history 目录，例如
   `/home/ubuntu/greatsellai-hr-staging-deployments`，仅部署用户可写。
4. DNS 将 `staging.hr.greatsellai.net` 继续指向旧 staging 服务器。迁移期间保留旧机现有
   Caddy 容器及其精确路由，不修改该 DNS、Caddy、staging 数据卷或 `.env.staging`。绝不让
   新生产主机接管该子域、`greatsellai.net` 根目录或泛域名。

新生产镜像刻意不再携带 staging 路由。部署/回滚到带旧路由的历史 Caddy 源码或镜像会被
生产发布脚本拒绝，避免新主机错误代理到自身不存在的 `172.17.0.1:18080`。

## 日常操作

1. 合并经过 CI 的 PR 到 `main`。
2. 等待 **Staging release** 自动运行；它会先做只读 Compose 预检，创建或复用
   `stg-YYYYMMDD-N` 标签，构建该 commit 的 API/Caddy 镜像并经 SSH 流式传输到 staging，
   部署后检查：`/health`、匿名 session、匿名原文件访问拒绝和 `/login` 页面。
3. 在 `https://staging.hr.greatsellai.net/login` 做业务验收。若 `main` 在验收期间前进，新的
   main 会生成新的 staging 候选；旧候选不能被晋级。
4. 在 Actions 手动运行 **Production promotion**，从 `main` 输入 `PROMOTE`。工作流会验证当前
   main 的唯一 `stg-*` tag、源码 archive SHA-256、staging release record（direct 记录核对
   API/Caddy image ID 与 revision）均一致，再创建 `prod-YYYYMMDD-N`，在生产机上以腾讯镜像
   参数构建 API/Caddy 镜像并部署。
5. 已有 `prod-*` 的重部署和回滚仍只用于恢复，不是绕过 staging 的新代码发布入口。

## 验收与故障处理

- **Staging release 失败**：不会创建可晋级生产的完成记录。修复 PR 后走新的 main；同一 commit
  可通过手动 `STAGE` 重试，已有同 SHA 的 `stg-*` 不会被移动。
- **Production promotion 失败**：常见于生产机上构建失败（依赖拉取、磁盘空间、Docker 资源）或
  发布校验失败，工作流会失败关闭。先修复根因（必要时为生产机扩容或清理镜像缓存）；若 `prod-*`
  标签已创建而部署未完成，可用 **Production deploy** 以同一标签重试。
- **Staging 不可达**：检查旧 staging 主机上原有的 Caddy 容器和 `172.17.0.1:18080` 私有
  监听是否仍在运行。新生产 Caddy 故意不包含 staging 路由；不要把 staging 域名、DNS 或
  旧机 Caddy 改到新生产主机来“修复”。
- **仅需要重试生产部署**：选择已有 `prod-*` 使用 **Production deploy**；不要手工伪造
  `stg-*` 或 `prod-*` 标签。
