# 生产主机与域名迁移（保留旧 staging）

## 目标和边界

生产入口迁移到新主机并使用 `https://hr.greatsellai.cn`。`staging.hr.greatsellai.net`、其旧主机、
数据库、文件卷、`.env.staging`、GitHub `staging` Environment 均保持不变。

旧主机上已经运行的 Caddy 暂时继续为 staging 提供既有精确路由。它是迁移桥接运行时，
不能停止、重建、清理卷或由新的 production 工作流管理。后续若要完全退役旧生产组件，
必须另开一次已审批的 staging edge 迁移，不与本次生产切换混在一起。

## 代码发布前置条件

按顺序合入并验证以下 PR：

1. 规范生产入口为 `hr.greatsellai.cn`；
2. 经 CI artifact 校验后把已验收的 staging 镜像传到独立生产主机；
3. 本 PR：新生产 Caddy 不再声明 staging 路由，生产工作流不再写旧 staging 网关，且旧路由
   的历史 Caddy tag 无法在新生产主机激活。

这三步只改变 GitHub 受控代码和自动化；不会连接服务器、读取环境变量、复制候选人资料或
修改 DNS。

## 人工迁移顺序

1. 保持旧主机的 staging Compose、Caddy、数据卷和 DNS 运行，不执行任何 staging 发布或清理。
2. 在新主机准备独立的生产目录、Docker、80/443 防火墙和生产 `.env.production`。不要复制
   staging 数据到生产，也不要让 staging 使用生产数据库或上传卷。
3. 按 [生产数据导入与首发](PRODUCTION_BOOTSTRAP_IMPORT.md) 先冻结旧生产的 API/worker 写入，重新生成并校验
   最终 production 四文件数据包，再导入新主机。旧 staging 始终保持运行；导入器只允许 `resume-screening-v3_postgres_data` 与
   `resume-screening-v3_uploads_data` 两个精确卷，且要求新主机尚无生产发布记录、运行时或目标卷。
   它不读取或复制 `.env.production`，也不会接触旧主机的任何 staging 资源。
4. 数据导入成功会留下 `bootstrap-import.env`。首次 Production promotion 仍使用已完成的 staging
   候选和精确 CI 镜像；它会先备份导入数据，再迁移并在新机本地验证，不会把生产数据导入当作绕过
   staging 门禁的发布入口。
5. 新生产 `.env.production` 的公开值设置为：
   - `RESUME_V3_DOMAIN=hr.greatsellai.cn`
   - `RESUME_V3_PUBLIC_APP_URL=https://hr.greatsellai.cn`
  在 DNS 切流和新的 Production promotion 前，如对应集成已启用，必须完成并核对以下外部服务配置：
  - 在 Google Cloud 和 Microsoft Entra 的应用注册中添加精确回调地址 `https://hr.greatsellai.cn/v1/mailbox-oauth/callback`；回调地址、`RESUME_V3_PUBLIC_APP_URL` 与实际发起授权的主入口必须完全一致。
  - 在腾讯云 SES 的已审核验证与重置密码模板中，将固定链接分别更新为 `https://hr.greatsellai.cn/verify-email?token={{token}}` 和 `https://hr.greatsellai.cn/reset-password?token={{token}}`。若平台生成新的模板 ID，同时更新生产环境中对应的模板 ID。
  私钥、数据库密码、AI/邮件/OCR 凭据继续只保存在新主机的忽略环境文件中。
6. 在 GitHub `production` Environment 中将 `PROD_DEPLOY_HOST`、SSH known-hosts、私钥和
   生产目录变量切到新主机。保留 `staging` Environment 的全部 `STAGING_*` 值不变。
7. 对已通过 staging 验收的当前候选运行受控 **Production promotion**，让它在新主机创建并部署
   `prod-*`。DNS 尚未切换时，首发只验证新机本地 API、匿名保护和 Caddy 配置；旧版本若含
   staging 网关会安全拒绝，而不是在新主机错误部署。
8. 仅在新机本地验证通过后，将 `hr.greatsellai.cn` 的 A/AAAA 记录切到新主机。不要把
   `greatsellai.net` 根域或泛域名指向 HR 项目。
9. DNS/TLS 生效后，按[生产数据导入与首发](PRODUCTION_BOOTSTRAP_IMPORT.md)中的“DNS/TLS 切流后的验收与交接”
   分别验证 `https://hr.greatsellai.cn/health` 和 `https://staging.hr.greatsellai.net/health`，记录
   `public_cutover_check=pending` 的人工交接结论。两者必须独立可用；生产发布不应改变 staging。

## ICP 备案检查

代码中的备案链接可在主站显示，但备案是否已经通过以腾讯云备案控制台和工信部查询结果为准。
域名、主体、服务器接入商或网站内容发生变化时，按接入商流程完成相应变更后再对外宣称备案
已生效。
