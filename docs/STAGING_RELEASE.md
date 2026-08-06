# 预发布与生产晋级

> 当前流程：staging 在美国发布 Runner 上直接构建该 commit 的镜像并经 SSH 流式传输到 staging 主机
> （不经中国 TCR），部署成功后只保留新 SHA 一套镜像。`main` CI 已停止向腾讯云 TCR 推送镜像，
> 生产晋级链当前暂停（其依赖的 TCR 镜像与 staging attestation 的 TCR 字段都不可得，会失败关闭而
> 不会误发）；将来重新接入生产镜像来源后再恢复。TCR 配置见 [TCR 发布镜像配置](TCR_RELEASE_SETUP.md)。
> 本说明优先于下文的历史 artifact/传输措辞。

## 发布链路

```text
PR 完整 CI
  → 合并 main
  → main CI 验证来源（main-release-provenance）
  → 自动构建 API / Caddy 镜像并流式传输到 staging，部署并执行公网 smoke
  → 人工运行 Production promotion（输入 PROMOTE，当前暂停）
  → 同一 SHA、同一 API/Caddy image ID 部署 production
```

生产不会因为 `main` 合并自动发布。当前生产晋级链暂停：`main` CI 不再推送 TCR 镜像，且 staging
的 direct 记录缺 TCR 字段，`verify-staging-release.sh` 会失败关闭，因此不会误发生产。预发布未
完成、冒烟检查失败、`main` 在验收期间前进、镜像 ID 不一致时 staging 也会失败关闭。

当前预发布和生产可以同机，也可以部署在不同 Docker 主机；它们始终是两个独立 Compose 项目。
生产晋级（恢复后）会从 `main` CI 的 TCR metadata 重新拉取 staging 已验收的同一镜像并校验
image ID，因此不依赖两端共享 Docker
镜像缓存。预发布应用以
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

`production` 的 `PROD_*` secrets/variables 必须只指向新生产主机；`staging` 的
`STAGING_*` 继续指向旧主机。建议为 `production` 配置 Required reviewer；这会让
**Production promotion** 在验证完 staging 后暂停，直到负责人批准。

`STAGING_DEPLOY_HOST` 与 `PROD_DEPLOY_HOST` 可以指向不同 Docker 主机。生产晋级只接受 staging
attestation 中的 API/Caddy image ID；它依赖从 `main` CI 归档的 TCR metadata artifact 与 staging
attestation 的 TCR 字段，两者当前都不可得，因此**生产晋级当前暂停、会失败关闭**。将来重新接入
生产镜像来源后再恢复，且绝不能在生产机重新构建镜像。

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
2. 等待 **Staging release** 自动运行；它会先做只读 Compose 预检，创建或复用 `stg-*` 标签，
   构建该 commit 的 API/Caddy 镜像并经 SSH 流式传输到 staging，部署后检查：`/health`、匿名
   session、匿名原文件访问拒绝和 `/login` 页面。
3. 在 `https://staging.hr.greatsellai.net/login` 做业务验收。若 `main` 在验收期间前进，新的
   main 会生成新的 staging 候选；旧候选不能被晋级。
4. 在 Actions 手动运行 **Production promotion**，从 `main` 输入 `PROMOTE`。工作流会验证当前
   main 的唯一 `stg-*` tag、源码 archive SHA-256、staging release record、运行中容器镜像 ID
以及 staging attestation 的 CI identity；工作流会重新下载、校验并传输同一镜像到生产主机，全部一致
才创建 `prod-*` 并以 `--prebuilt-images` 部署。
5. 已有 `prod-*` 的重部署和回滚仍只用于恢复，不是绕过 staging 的新代码发布入口。

## 验收与故障处理

- **Staging release 失败**：不会创建可晋级生产的完成记录。修复 PR 后走新的 main；同一 commit
  可通过手动 `STAGE` 重试，已有同 SHA 的 `stg-*` 不会被移动。
- **Production promotion**：当前暂停（`main` CI 不再推 TCR、staging 为 direct 记录），会失败关闭。
  不要在生产机上 build 来“补救”，这会破坏同一镜像保证。恢复前需先接入新的生产镜像来源并适配
  staging attestation。
- **Staging 不可达**：检查旧 staging 主机上原有的 Caddy 容器和 `172.17.0.1:18080` 私有
  监听是否仍在运行。新生产 Caddy 故意不包含 staging 路由；不要把 staging 域名、DNS 或
  旧机 Caddy 改到新生产主机来“修复”。
- **仅需要重试生产部署**：选择已有 `prod-*` 使用 **Production deploy**；不要手工伪造
  `stg-*` 或 `prod-*` 标签。
