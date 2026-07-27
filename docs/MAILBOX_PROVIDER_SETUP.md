# 邮箱服务商接入说明

本文说明工作区管理员如何把招聘收件邮箱接入 GreatSell AI 招聘工具，以及部署管理员如何启用 Google 和 Microsoft 的 OAuth。它只涵盖收取简历附件，不涉及自动回复、拒信、邀约或发件邮箱配置。

> 当前交付边界：服务商目录、IMAP/OAuth 后端、迁移、安全测试与邮箱设置前端均已接入同一变更集。工作区管理员在“设置 → 收件邮箱”中选择服务商，再按页面引导使用授权码或 Google/Microsoft 网页授权；页面不会暴露或接收任意 IMAP 主机、端口或 OAuth 密钥。

> 生产启用前提：`compose.yml` 的共享运行环境已透传本文列出的邮箱变量给 API、Worker 和 Migrate；部署负责人只需在被忽略的生产环境文件中配置真实值。若 Gmail / Microsoft 的完整 OAuth 三元组未配置，服务商列表会显示为“不可用”，不会半配置后误发起授权。

## 使用前须知

- 一个工作区可以创建多个有业务名称的收件通道，例如“算法社招”“校园招聘”。通道之间的邮件起点、同步状态、附件审计和凭据彼此独立。
- 新建通道时只会接收**绑定完成之后**的新邮件；不会补扫历史邮件。
- 管理员在页面中选择经审核的服务商，系统固定 IMAPS 主机和 TLS `993` 端口。页面不允许填写任意主机、端口或 IP 地址。
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
3. 填写接收邮箱和文件夹。通常使用 `INBOX`；如果服务商对文件夹名称有特殊要求，按该服务商显示的实际名称填写。
4. 按服务商类型完成授权：
   - 授权码服务商：粘贴**专用授权码/客户端专用密码**，不要粘贴网页登录密码。
   - OAuth 服务商：点击对应的 Google 或 Microsoft 授权按钮，在服务商登录页完成授权后自动返回本系统。
5. 系统验证 IMAP 连接并记录当前位置。从此刻起同步新邮件附件。

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

- 服务商端点由后端目录控制；即使是兼容期内的旧 IMAP API，也必须通过精确主机白名单、TLS、DNS 重绑定和私网地址校验，不能成为任意网络访问能力。
- OAuth `state` 是一次性、短期有效值，并绑定发起授权的当前工作区、当前管理员和成员关系。其他工作区或其他账号拿到同一回调链接也只能安全失败，且不会消耗原管理员的授权意图。
- 浏览器回调不会显示授权 code、access token、refresh token 或服务商错误详情；完成后只会跳回收件页面并显示成功或失败状态。
- OAuth refresh token 与授权码服务商的凭据分表保存、加密静态存储；短期 access token 仅在单次 IMAP 认证内存中使用。
- 邮件正文、发件人地址和非必要邮件头不会进入简历工作台或模型提示词。系统只处理允许格式的附件，并保持附件、简历、AI 任务和收件配置的工作区隔离。

## 常见问题

### 页面显示服务商不可用

飞书、腾讯企业邮箱和 QQ 邮箱需要部署环境允许对应固定端点。Gmail 和 Microsoft 365 除端点允许外，还必须配置完整的 OAuth client ID、client secret 和回调地址。请联系部署管理员，不要尝试填写自定义服务器地址绕过限制。

### 授权后仍显示需要重新授权

常见原因是服务商撤销了应用授权、管理员策略变更、客户端专用密码失效或邮箱侧关闭了 IMAP。先确认服务商侧 IMAP/OAuth 设置，再在本系统点击“重新授权”；不要新建同名通道覆盖原通道。

### 为什么没有导入已有邮件

这是设计行为。每个通道在绑定成功时记录 IMAP 当前水位线，只接收之后到达的新邮件，避免把历史邮箱内容或无关附件批量导入招聘库。

### 能接入未列出的邮箱服务商吗

不能直接通过页面输入服务器地址。新增服务商需要先评审其固定端点、TLS/OAuth 能力、授权方式和安全边界，再由产品和部署团队把它加入服务商目录与服务端精确白名单。
