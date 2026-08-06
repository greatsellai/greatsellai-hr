# TCR 发布镜像配置

自动发布不再把数百 MB 的 Docker 镜像作为 GitHub Actions artifact 下载并经 SSH 转发。`main` CI 将 API 和 Caddy 镜像推送到腾讯云 TCR，预发布和生产只按不可变 `repo@sha256:<manifest>` 拉取同一份镜像。

## 一次性配置

在腾讯云个人版 TCR 的广州实例中创建私有命名空间和仓库：

- 命名空间：`greatsellaihr`
- 仓库：`hr-api` 与 `hr-caddy`

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 配置仓库级值：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| Variable | `TCR_REGISTRY` | `ccr.ccs.tencentyun.com` |
| Variable | `TCR_NAMESPACE` | `greatsellaihr` |
| Secret | `TCR_USERNAME` | TCR Docker 登录用户名 |
| Secret | `TCR_PASSWORD` | TCR 初始化密码或访问凭证 |

这些是 TCR Docker 登录凭据，不是腾讯云 API 的 SecretId/SecretKey。不要把它们提交到仓库、写进服务器 `.env.production` 或贴进 Actions 日志。

## 发布时发生的事

1. `main` CI 构建并完成运行时回归后，使用唯一的 `ci-<sha>-<run>-<attempt>` 标签推送两个镜像到 TCR。
2. CI 解析每个镜像的 manifest digest，并只保存很小的 metadata artifact（提交、CI run、OCI label、manifest digest、config digest 和校验和）。
3. Staging 校验 metadata 后，目标主机通过 stdin 登录 TCR，并在临时且自动清理的 Docker 凭据目录中拉取精确 digest；它会复核 OCI revision、CI run/attempt、config digest 与 `RepoDigests`，再部署。
4. Production promotion 读取已完成的 staging attestation，按完全相同的两个 digest 拉取并复核后才创建 `prod-*` 标签和部署。

生产仍需在 GitHub Actions 手动运行 **Production promotion** 并输入 `PROMOTE`；合并 `main` 不会自动连接或部署生产。

## 故障排查

- CI 中提示缺少 `TCR_*`：检查它们是否为**仓库级** Actions variable/secret，名称完全一致。
- CI 报 `no scope specify`：发布链路会写入临时 `DOCKER_CONFIG`，让 Docker 为实际仓库请求带 scope 的 token；确认 `TCR_USERNAME` 和 `TCR_PASSWORD` 是个人版 TCR 登录凭据，而不是腾讯云 API 密钥。
- `docker pull` 被拒绝：确认实例地域是广州、仓库与命名空间名称准确，并重新初始化或更新 TCR Docker 登录密码。
- TCR 中没有对应仓库：创建私有 `hr-api`、`hr-caddy` 仓库后重新运行该次 main CI；不要在服务器本地重新 build 来补救。
- metadata 或 digest 不一致：流程会安全失败。修正配置后从 main CI 重新走 staging 验收，不应手动替换镜像标签。
