# 邮箱服务商接入说明

本文说明工作区管理员如何把招聘收件邮箱接入 GreatSell AI 招聘工具，以及部署管理员如何启用 Google 和 Microsoft 的 OAuth。它只涵盖收取简历附件，不涉及自动回复、拒信、邀约或发件邮箱配置。

> 当前交付边界：服务商目录、IMAP/OAuth 后端、迁移、安全测试与邮箱设置前端均已接入同一变更集。工作区管理员可选择预设服务商，也可选择“通用 IMAP 邮箱”并填写邮箱服务商提供的 IMAP 域名；页面不会暴露 OAuth 密钥或任何已保存的授权码。

> 生产启用前提：`compose.yml` 的共享运行环境已透传本文列出的邮箱变量给 API、Worker 和 Migrate；部署负责人只需在被忽略的生产环境文件中配置真实值。若 Gmail / Microsoft 的完整 OAuth 三元组未配置，服务商列表会显示为“不可用”，不会半配置后误发起授权。

## 使用前须知

- 一个工作区可以创建多个有业务名称的收件通道，例如“算法社招”“校园招聘”。通道之间的邮件起点、同步状态、附件审计和凭据彼此独立。
- 新建通道默认只接收**绑定完成之后**的新邮件。管理员也可在首次绑定时选择有限的“最近 N 天”范围；该范围由服务端冻结并在后台分批完成，之后始终只同步新邮件，不能通过编辑或重新授权扩大历史扫描。
- 预设服务商由系统固定 IMAPS 主机和 TLS `993` 端口。通用 IMAP 只允许填写服务器**域名**，端口仍固定为 `993`；不接受 IP 地址、网址、内网地址或其他端口。
- 通用 IMAP 在保存和每次同步时都会重新校验域名解析结果、TLS 证书和 DNS 重绑定风险。仅通过校验的公网 IMAPS 服务可连接。
- 授权码、客户端专用密码和 OAuth refresh token 只加密保存在服务端。页面、API 响应、日志和 Agent 工具均不会回显这些内容。
- 新通道可以修改名称、暂停、恢复或归档。已经产生入库记录的通道不能静默改为另一邮箱，以免把新邮件混入旧来源。
- 服务商也不能在原通道上切换；请新建目标服务商通道，避免把旧服务商凭据发送到另一服务商。

## 部署前置变量与回调入口

以下变量只保存于部署环境，不能写入仓库、前端构建产物、工单截图或聊天记录。`api`、`worker` 和 `migrate` 必须拿到相同的值：

```text
RESUME_V3_EMAIL_CREDENTIALS_KEY
RESUME_V3_MAILBOX_IMAP_ALLOWED_HOSTS=imap.feishu.cn,imap.exmail.qq.com,imap.qq.com,imap.gmail.com,outlook.office365.com
RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_ID
RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_SECRET
RESUME_V3_MAILBOX_GOOGLE_OAUTH_REDIRECT_URI
RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_ID
RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_SECRET
RESUME_V3_MAILBOX_MICROSOFT_OAUTH_REDIRECT_URI
```

生产回调统一使用主入口：

```text
https://hr.greatsellai.net/v1/mailbox-oauth/callback
```

兼容入口 `https://greatsellai.net/greatsellhr/` 可以发起授权；浏览器会用短期、安全 Cookie 把流程交给上述主入口，完成后落在 `hr.greatsellai.net`。OAuth 不会把授权 code、state、token 或错误详情带回前端 URL。若部署把主入口或回调地址改为不属于同一受控域名的地址，后端会安全拒绝启动，而不是创建无法完成的授权。

页面工作方式：

1. 读取 `GET /v1/mailbox-providers`，按 `available` 和 `authentication_mode` 展示服务商。
2. `POST /v1/mailbox-oauth/start` 或 `POST /v1/mailboxes/{mailbox_id}/oauth/reauthorize` 得到 `authorization_url` 后，使用整页跳转。
3. 回调完成后会 303 到 `?mailbox_oauth=connected|failed` 和 `#settings/mailbox`；前端刷新 `GET /v1/mailboxes` 后清理 query。
4. `authorization_status` 的值为 `not_connected`、`connected`、`reauthorization_required` 或 `unavailable`；浏览器永远不接触 refresh token 或授权码。

## 工作区管理员：创建收件通道

1. 以工作区管理员身份进入“设置 → 收件邮箱”。
2. 点击“新建收件通道”，填写通道名称并选择服务商。
3. 选择首次入库范围。默认“从现在开始”不导入历史邮件；选择“最近 N 天”时，IMAP 按自然日回溯，最近 7 天表示当天与此前 6 个自然日。
4. 填写接收邮箱。系统固定同步收件箱（`INBOX`）。
5. 按服务商类型完成授权：
   - 授权码服务商：粘贴**专用授权码/客户端专用密码**，不要粘贴网页登录密码。
   - OAuth 服务商：点击对应的 Google 或 Microsoft 授权按钮，在服务商登录页完成授权后自动返回本系统。
6. 系统验证 IMAP 连接并记录 UIDNEXT 与 UIDVALIDITY。若选择了历史范围，先在后台导入该范围内的附件；完成后只同步新邮件。

如果授权失效，收件通道会显示需要重新授权。重新授权不会改变已有简历来源、附件记录或收件水位线。

## 支持的服务商

### 飞书邮箱

- 在飞书邮箱侧由管理员开启第三方邮箱客户端登录。
- 为该邮箱生成飞书专用密码，在创建通道时粘贴该专用密码。
- 系统使用固定端点 `imap.feishu.cn:993`（TLS）。
- 不要使用飞书账号的网页登录密码。

### 腾讯企业邮箱

- 确认企业邮箱已启用 IMAP/第三方客户端访问。
- 如企业启用了安全登录或客户端专用密码，优先使用客户端专用密码；否则按企业邮箱管理员的 IMAP 登录策略填写允许的邮箱凭据。
- 系统使用固定端点 `imap.exmail.qq.com:993`（TLS）。
- 若企业策略禁止第三方客户端，请由企业邮管理员放行后再连接。

### QQ 邮箱

- 在 QQ 邮箱设置中开启 IMAP 服务，并按 QQ 邮箱页面提示生成授权码。
- 在系统中填写该授权码，而不是 QQ 登录密码。
- 系统使用固定端点 `imap.qq.com:993`（TLS）。

### Gmail / Google Workspace

- 使用 Google OAuth 授权，系统不会收集或保存 Google 登录密码。
- Gmail 用户需确认已开启 IMAP；Google Workspace 用户还需确认管理员没有禁止 IMAP 或第三方 OAuth 应用访问。
- 授权页面中的 Google 账号应当就是要收取简历的邮箱，或拥有该邮箱 IMAP 访问权的账号。
- 系统使用固定端点 `imap.gmail.com:993`（TLS）和 XOAUTH2，不使用 IMAP `LOGIN`。
- 如果页面提示“尚未配置”，请联系部署管理员完成下方的 Google OAuth 配置。

### Microsoft 365 / Outlook

- 使用 Microsoft OAuth 授权，系统不会收集或保存 Microsoft 登录密码。
- Microsoft 365 管理员可能需要允许用户同意该应用权限，或先代表租户授予管理员同意。
- 系统使用固定端点 `outlook.office365.com:993`（TLS）和 XOAUTH2，不使用 IMAP `LOGIN`。
- 如果页面提示“尚未配置”，请联系部署管理员完成下方的 Microsoft OAuth 配置。

### 通用 IMAP 邮箱

- 适用于未列出但提供标准 SSL/TLS IMAP 的企业邮箱，例如自建企业邮或其他国内外邮箱服务商。
- 选择“通用 IMAP 邮箱”后，填写服务商文档提供的 **IMAP 服务器域名**、接收邮箱和专用授权码或客户端密码；系统固定同步收件箱（`INBOX`）。
- 系统固定使用 `993` 端口和 SSL/TLS。若服务商仅提供 `143 + STARTTLS`、网页登录授权或 IP 地址，当前不能接入。
- 保存时会立即验证连接；同步时会再次校验公网 DNS、地址安全性和服务器证书。授权码始终只加密保存在服务端。

## 部署管理员：启用 Google OAuth

以下配置只由部署管理员在服务端环境变量中完成。不要把 client secret 写入仓库、前端、截图、工单或聊天记录。

1. 在 Google Cloud Console 创建或选择一个项目，配置 OAuth 同意屏幕。
2. 创建“Web application”类型的 OAuth Client。
3. 将授权重定向地址精确填写为：

   ```text
   https://<当前 HR 站点>/v1/mailbox-oauth/callback
   ```

   本地开发可以使用本地的同路径回调地址；生产必须使用 HTTPS。
4. 确保同意屏幕和 Workspace 管理策略允许该应用请求 `https://mail.google.com/` IMAP 访问；系统不请求或使用 Google 个人资料权限。
5. 仅在服务器环境中配置以下三个变量，并重启/按发布流程部署 API 与 Worker：

   ```text
   RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_ID
   RESUME_V3_MAILBOX_GOOGLE_OAUTH_CLIENT_SECRET
   RESUME_V3_MAILBOX_GOOGLE_OAUTH_REDIRECT_URI
   ```

6. 在一个非生产测试邮箱完成授权、绑定、同步和重新授权测试后，再向工作区开放。

Google 的 OAuth 同意屏幕处于测试状态时，只有已加入测试用户的账号可以授权；面向真实用户前应按 Google 的要求完成发布和敏感权限审核。

## 部署管理员：启用 Microsoft OAuth

1. 在 Microsoft Entra 管理中心创建 App registration。
2. 配置 Web 重定向地址：

   ```text
   https://<当前 HR 站点>/v1/mailbox-oauth/callback
   ```

3. 为应用添加 Exchange Online 的委派 IMAP 权限 `IMAP.AccessAsUser.All`。如果租户要求管理员同意，请在 Entra 中完成管理员同意。
4. 创建 client secret，并仅在服务器环境中配置以下三个变量：

   ```text
   RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_ID
   RESUME_V3_MAILBOX_MICROSOFT_OAUTH_CLIENT_SECRET
   RESUME_V3_MAILBOX_MICROSOFT_OAUTH_REDIRECT_URI
   ```

5. 按发布流程重启/部署 API 与 Worker，再用独立测试邮箱完成绑定、同步和重新授权验证。

系统会请求 `offline_access` 和 `https://outlook.office.com/IMAP.AccessAsUser.All`，用于获得可续期的 IMAP 访问；不会用 OAuth 权限发送邮件。

## 安全与隔离边界

- 预设服务商和兼容期内的旧 IMAP API 继续通过精确主机白名单校验。通用 IMAP 只接受域名、TLS `993` 和公网 DNS 结果，并使用已验证地址建立 TLS 连接、保留原域名 SNI 和证书校验；IP、私网/回环地址、混合 DNS 结果、DNS 重绑定和其他端口均会被拒绝。
- OAuth `state` 是一次性、短期有效值，并绑定发起授权的当前工作区、当前管理员和成员关系。其他工作区或其他账号拿到同一回调链接也只能安全失败，且不会消耗原管理员的授权意图。
- 浏览器回调不会显示授权 code、access token、refresh token 或服务商错误详情；完成后只会跳回收件页面并显示成功或失败状态。
- OAuth refresh token 与授权码服务商的凭据分表保存、加密静态存储；短期 access token 仅在单次 IMAP 认证内存中使用。
- 邮件正文、发件人地址和非必要邮件头不会进入简历工作台或模型提示词。系统只处理允许格式的附件，并保持附件、简历、AI 任务和收件配置的工作区隔离。

## 常见问题

### 页面显示服务商不可用

飞书、腾讯企业邮箱和 QQ 邮箱需要部署环境允许对应固定端点。Gmail 和 Microsoft 365 除端点允许外，还必须配置完整的 OAuth client ID、client secret 和回调地址。其他标准 IMAPS 服务可选择“通用 IMAP 邮箱”；如服务商不支持 SSL/TLS `993` 或其地址安全校验失败，则当前不能接入。

### 授权后仍显示需要重新授权

常见原因是服务商撤销了应用授权、管理员策略变更、客户端专用密码失效或邮箱侧关闭了 IMAP。先确认服务商侧 IMAP/OAuth 设置，再在本系统点击“重新授权”；不要新建同名通道覆盖原通道。

### 为什么没有导入已有邮件

默认“从现在开始”是设计行为：系统记录 IMAP 当前水位线，只接收之后到达的新邮件，避免把历史邮箱内容或无关附件批量导入招聘库。如需有限回溯，应在新建通道时选择“最近 N 天”；该选择只在首次绑定生效，完成后不会重复扫描历史邮件。

### 能接入未列出的邮箱服务商吗

可以选择“通用 IMAP 邮箱”并填写邮箱服务商提供的服务器域名，前提是服务商支持 SSL/TLS `993` 与授权码或客户端密码。常用服务商仍建议使用预设卡片，以获得更具体的配置提示。需要 OAuth、非标准端口或特殊认证机制的服务商，仍需新增专用接入。
