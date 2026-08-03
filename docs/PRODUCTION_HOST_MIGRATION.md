# 生产主机与域名迁移（保留旧 staging）

## 目标和边界

生产入口迁移到新主机并使用 `https://hr.greatsell.cn`。`staging.hr.greatsellai.net`、其旧主机、
数据库、文件卷、`.env.staging`、GitHub `staging` Environment 均保持不变。

旧主机上已经运行的 Caddy 暂时继续为 staging 提供既有精确路由。它是迁移桥接运行时，
不能停止、重建、清理卷或由新的 production 工作流管理。后续若要完全退役旧生产组件，
必须另开一次已审批的 staging edge 迁移，不与本次生产切换混在一起。

## 代码发布前置条件

按顺序合入并验证以下 PR：

1. 规范生产入口为 `hr.greatsell.cn`；
2. 经 CI artifact 校验后把已验收的 staging 镜像传到独立生产主机；
3. 本 PR：新生产 Caddy 不再声明 staging 路由，生产工作流不再写旧 staging 网关，且旧路由
   的历史 Caddy tag 无法在新生产主机激活。

这三步只改变 GitHub 受控代码和自动化；不会连接服务器、读取环境变量、复制候选人资料或
修改 DNS。

## 人工迁移顺序

1. 保持旧主机的 staging Compose、Caddy、数据卷和 DNS 运行，不执行任何 staging 发布或清理。
2. 在新主机准备独立的生产目录、Docker、80/443 防火墙、生产 `.env.production` 和生产数据
   副本。不要复制 staging 数据到生产，也不要让 staging 使用生产数据库或上传卷。
3. 新生产 `.env.production` 的公开值设置为：
   - `RESUME_V3_DOMAIN=hr.greatsell.cn`
   - `RESUME_V3_PUBLIC_APP_URL=https://hr.greatsell.cn`
   私钥、数据库密码、AI/邮件/OCR 凭据继续只保存在新主机的忽略环境文件中。
4. 在 GitHub `production` Environment 中将 `PROD_DEPLOY_HOST`、SSH known-hosts、私钥和
   生产目录变量切到新主机。保留 `staging` Environment 的全部 `STAGING_*` 值不变。
5. 对已通过 staging 验收的当前候选运行受控 **Production promotion**，让它在新主机创建并部署
   `prod-*`；检查 `/health`、登录、匿名原文件拒绝、worker 和 TLS。旧版本若含 staging 网关会
   安全拒绝，而不是在新主机错误部署。
6. 仅在新主机健康检查通过后，将 `hr.greatsell.cn` 的 A/AAAA 记录切到新主机。不要把
   `greatsellai.net` 根域或泛域名指向 HR 项目。
7. 分别验证 `https://hr.greatsell.cn/health` 和
   `https://staging.hr.greatsellai.net/health`。两者必须独立可用；生产发布不应改变 staging。

## ICP 备案检查

代码中的备案链接可在主站显示，但备案是否已经通过以腾讯云备案控制台和工信部查询结果为准。
域名、主体、服务器接入商或网站内容发生变化时，按接入商流程完成相应变更后再对外宣称备案
已生效。
