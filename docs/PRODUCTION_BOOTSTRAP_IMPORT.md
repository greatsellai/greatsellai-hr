# 新生产主机：生产数据导入与首发

本流程只用于把既有**生产** PostgreSQL 数据和原始简历文件迁入一台尚未承载过 GreatSell HR
生产运行时的新主机。它不是日常发布、不是 staging 发布，也不替代已有的生产备份/回滚流程。

## 不可跨越的边界

- 只允许写入两个精确生产卷：
  `resume-screening-v3_postgres_data` 与 `resume-screening-v3_uploads_data`。
- 绝不读取、传输、输出或覆盖 `.env.production`；新主机的环境文件由主机管理员本地维护。
- 不会创建、停止、删除或重命名任何 `resume-screening-v3-staging_*` 资源，也不会操作旧 staging
  主机、其 Caddy、数据卷、环境文件或 DNS。
- 不接受已经有 `current-release.env`、`pending-release.env`、既有生产 Compose 容器或任一目标卷
  的主机。遇到这些状态会失败关闭，不会猜测或覆盖现有数据。
- 导入本身不发布应用代码。之后的首个生产上线仍只能通过已完成 staging 验证的标准
  **Production promotion**。

## 冻结旧生产并生成最终快照

导入包必须是 **切换窗口内最后一次** 从旧生产生成的快照，而不是早先的测试备份或 staging 数据。操作顺序固定如下：

1. 确认旧主机上的 staging 完全独立；本步骤绝不停止、重启或导出 staging。
2. 暂停旧生产的 `api` 和 `worker` 写入入口，保留旧生产数据库和上传卷，直到最终快照校验完成。不要把停止命令泛化到 Compose 项目或 Caddy。
3. 从旧生产的两个精确卷生成 `database.dump` 与 `uploads.tar.gz`，写入本文件规定的 `checksums.sha256` 和 `manifest.env`；重新校验后再恢复旧生产 API/worker，或保持旧生产只读直至 DNS 切流决定。
4. 只把重新生成且校验通过的四文件快照传到私有对象存储和新主机 incoming 目录。不能复用旧的、字段不完整的备份 manifest，也不能把 GitHub artifact 当作候选人数据通道。

旧生产停写到最终快照完成之间若出现异常，立即恢复旧生产 API/worker；此时不要开始新主机导入。这样既避免漏掉最后一批简历，也不会影响旧 staging。

## 可移植数据包契约

跨地域传输完成后，数据包必须被放到新生产机的：

```text
<PROD_HISTORY_DIR>/incoming/<import-id>/
```

目录只能包含下列四个普通文件，不能包含链接、额外文件或环境文件：

```text
database.dump
uploads.tar.gz
checksums.sha256
manifest.env
```

`database.dump` 是 PostgreSQL custom-format dump；`uploads.tar.gz` 只包含目录和普通原文件，
不允许符号链接、硬链接、设备、FIFO 或路径穿越。`checksums.sha256` 必须恰好是以下两行的
SHA-256（顺序固定）：

```text
<database.dump sha256>  database.dump
<uploads.tar.gz sha256>  uploads.tar.gz
```

`manifest.env` 是非敏感来源声明，必须含有：

```text
format_version=1
state=complete
snapshot_kind=production_bootstrap
import_id=<import-id>
source_environment=production
source_compose_project=resume-screening-v3
database_file=database.dump
uploads_file=uploads.tar.gz
created_at=YYYY-MM-DDTHH:MM:SSZ
```

可选的 `source_release_tag` 和 `source_release_commit` 都会被格式校验；两者同时存在时，tag 的
短 SHA 必须与 commit 前缀一致。不要在 manifest 中写入账号、密码、连接串、候选人姓名、文件名
或任何凭据。

建议将数据包先保存到私有对象存储，使用校验和验证后，再下载到上述 incoming 目录。对象存储
仅承担传输，不会进入 GitHub artifact、Git 历史或 Actions 日志。

## 导入步骤

1. 确认旧主机的 production 与 staging 仍独立运行；导出时只选 production 的两个精确卷。
2. 在新主机放好 `.env.production`，但不要复制旧机环境文件、不要启动应用 Compose 服务。
3. 将经过校验的数据包下载到 `incoming/<import-id>`。此时不要手工创建任何 production 卷。
4. 从 `main` 手动运行 GitHub Actions **Production bootstrap data import**，输入该 ID 和
   `IMPORT_PRODUCTION_SNAPSHOT`。也可在干净、与 `origin/main` 完全一致的工作区运行：

   ```bash
   scripts/bootstrap-import-production.sh <import-id> \
     --host "$PROD_DEPLOY_HOST" \
     --project-dir "$PROD_PROJECT_DIR" \
     --history-dir "$PROD_HISTORY_DIR" \
     --ssh-key /path/to/deploy-key \
     --confirm-import
   ```

5. 导入器重新校验 manifest、两份 SHA-256、PostgreSQL dump 和 uploads archive，启动一个只含
   PostgreSQL 的短生命周期容器完成恢复，再停止并移除这个容器。它把已验证数据包移到
   `bootstrap-imports/<import-id>`，并写入可审计的 `bootstrap-import.env` 标记。
6. 运行标准 **Production promotion**。它仍要求当前 main 的 staging attestation、精确 CI 镜像和
   `PROMOTE`；导入标记仅允许这一次首发将已存在的两个生产卷作为迁移前数据，而不是把数据导入
   变成绕过发布门禁的入口。

## 首发、DNS 与验证

导入标记被首个生产发布消费时，发布器先将标记从 `ready` 原子变为 `deploy_attempted`，再启动短生命周期
数据库创建一份新的数据库+上传卷成对备份，随后执行迁移和启动。这样即使备份阶段中断，首发也不会被静默重试或误当成空环境。

新主机尚未接管域名时，公网 `https://<domain>` 仍可能解析到旧主机。因此首发阶段发布器验证的是
**新主机本机**的 API 健康、匿名会话保护、原文件 401 保护、Caddy 配置以及私有 Caddy→API 反代链路，而不会把旧主机的
公网响应当成新主机健康。首发记录会标记 `runtime_verification=bootstrap_target_local` 和
`public_cutover_check=pending`。前者表示新主机本地运行时已通过；后者**不是** DNS/TLS 已验收，也不是发布失败，
而是把公网切流验收明确交给切流负责人完成。不要手工改写、删除或把这条首发记录中的 `pending` 改成 `pass`；
后续正常生产晋级会在自己的新发布记录中写入 `public_cutover_check=pass`，不会倒改这次首发记录。

只有本机验证成功后才可切换 HR 子域名 DNS。切换前在变更单或受控运行记录中保存以下**非敏感**信息，作为验收和回切依据：

- 首发的 `prod-*` 标签与 commit、切流开始时间；
- HR 子域名切换前的 A/AAAA 值与 TTL、切换后的目标地址；
- 负责人的验收结论和回切结论。

不要在该记录中保存部署私钥、环境变量、会话 Cookie、候选人姓名、简历文件名、原件 URL 或接口响应正文。

## DNS/TLS 切流后的验收与交接

`public_cutover_check=pending` 必须持续到下列**公网**验收全部完成。DNS 传播尚处于记录 TTL 内时，只能视为传播中，
不能把本机缓存或旧主机响应当作通过；应从至少一个独立解析器和浏览器网络确认目标已经是新主机。

1. 检查 HR 子域名的 A/AAAA 解析和 TLS 证书，确认它命中新生产地址、证书域名正确，且没有把根域、泛域名或 staging 域名映射到 HR。
2. 通过公网 HTTPS 执行健康检查，而不是直接访问容器或新机回环地址：

   ```bash
   curl --fail --silent --show-error "https://<production-hr-domain>/health"
   curl --fail --silent --show-error "https://<production-hr-domain>/v1/auth/session"
   ```

   `/v1/auth/session` 在未登录状态应返回 `200`，且响应表达 `authenticated=false` 和 `login_required=true`。
3. 不带 Cookie 请求一个固定的、不会对应真实候选人的占位 ID，确认原文件访问仍先被鉴权层拒绝为 `401`：

   ```bash
   curl --silent --show-error --output /dev/null --write-out "%{http_code}\n" \
     "https://<production-hr-domain>/v1/resumes/cutover-auth-check/original-file"
   ```

4. 使用浏览器完成一次真实登录、候选人列表读取和已授权原文件访问抽检；只记录通过/失败，不把候选人内容带入公开日志或变更单。
5. 从独立入口检查 staging 的页面和 `/health`，确认生产切流没有改变 staging；同时确认旧生产仍可作为回切目标保留。
6. 将以上检查时间、解析器/浏览器网络、通过结果和最终结论写入变更记录。至此才可宣布公网切流完成；
   `pending` 保留在首发历史中，作为“首发在 DNS 之前完成本地验证、随后已按本清单人工验收”的可审计交接点。

### 公网验收失败时的回切

若 TTL 传播窗口结束后，任一项验收失败、HTTPS 仍命中旧地址、TLS 证书不正确、匿名保护不符合预期，或 staging 受到影响，
立即执行以下回切，而不是先修改新机数据：

1. 仅将 **HR 子域名** A/AAAA 回切到切换前记录的旧生产值；不修改根域、泛域名、staging DNS、Caddy 配置或 Docker 卷。
2. 等待回切 DNS 生效后，从公网复测旧生产的 `/health`、匿名会话和未登录原文件 `401`，并记录回切完成时间。
3. 保留新主机已导入的数据、发布记录和非敏感错误证据以便排查；DNS 回切只恢复流量，绝不删除数据。
4. 不要仅因公网 DNS/TLS 验收失败运行 bootstrap restore。该恢复只适用于首发在本机验证前失败且没有
   `current-release.env` 的情形；其余情况先按本节回切，再走受审查的故障处理或新的生产发布。

## 首发失败恢复

若首个 promotion 在迁移、启动或本机验证后失败，`bootstrap-import.env` 保持
`deploy_attempted`，任何普通部署都会拒绝再次消费它。不要重新导入、不要删除卷，也不要手工
清理 pending 标记。

若日志显示本机验证和 `current-release.env` 已成功写入、但 runner 在清理 `pending-release.env` 前中断，
这不是需要回滚的数据失败：使用现有手动 Actions **Production healthy pending finalization**，输入
`FINALIZE_HEALTHY_PENDING_RUNTIME`。该入口只接受同一 tag/commit、同一 `bootstrap_import_id`、空首发前序和
`runtime_verification=bootstrap_target_local` 的精确记录；它会再次执行新主机私有 Caddy→API 验证并归档 pending，
不会在 DNS 尚未切换时请求公网域名。若这些前提不满足，保持失败关闭并按下面的恢复流程处理。

在确认没有 `current-release.env` 后，使用手动 Actions **Production bootstrap data restore** 并输入
`RESTORE_PRODUCTION_BOOTSTRAP`，或运行：

```bash
scripts/restore-production-bootstrap.sh <import-id> \
  --host "$PROD_DEPLOY_HOST" \
  --project-dir "$PROD_PROJECT_DIR" \
  --history-dir "$PROD_HISTORY_DIR" \
  --ssh-key /path/to/deploy-key \
  --confirm-restore
```

恢复器只停止和删除精确 production Compose 服务容器，使用保存的数据包恢复两个 production 卷，
归档本次 pending 记录，并把 marker 恢复为 `ready`。它不接触 staging 资源、Caddy 数据卷、环境文件
或 DNS。恢复失败同样保持失败关闭，需人工检查后再尝试。
