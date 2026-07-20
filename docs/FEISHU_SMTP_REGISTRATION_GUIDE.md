# 飞书 SMTP 注册验证邮件接入说明

适用对象：负责 GreatSell AI 招聘工具账号注册、找回密码或其他事务邮件的同事。

本文描述当前临时使用的飞书 SMTP 方案。它只负责向用户发送注册验证邮件；收取简历附件的 IMAP 配置是另一条链路，不能混用账号或凭据。

## 1. 已实现的注册邮件链路

```text
用户提交注册
  → 创建用户、工作区和 30 天试用
  → 生成一次性邮箱验证令牌（数据库仅保存摘要）
  → 飞书 SMTP 发送验证链接
  → 用户打开 /verify-email
  → 后端核验令牌并放开工作区业务访问
```

未验证的账号不能访问简历、候选人、JD、评分或 AI 功能。邮箱验证成功后才进入工作台。

相关接口：

- `POST /v1/auth/register`
- `POST /v1/auth/email-verification/complete`
- `POST /v1/auth/email-verification/resend`
- `GET /v1/auth/session`

## 2. 飞书侧需要准备什么

建议创建专用的飞书公共邮箱，例如 `noreply@你的域名`。当前测试可以使用现有公共邮箱，但不要把简历收件邮箱的 IMAP 凭据复用为注册发信凭据。

在飞书邮箱后台为该发件邮箱开启第三方客户端/SMTP，并生成专用密码。当前飞书参数为：

```text
SMTP 主机：smtp.feishu.cn
SMTP 端口：465
加密方式：SSL（隐式 TLS）
用户名：完整发件邮箱地址
密码：飞书生成的专用密码
```

也支持 STARTTLS，但必须使用 587 端口；不要使用 25 端口。

## 3. 生产环境变量

以下变量放在服务器的忽略文件 `.env.production` 中，不提交到 Git：

```dotenv
RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER=feishu_smtp
RESUME_V3_TRANSACTIONAL_EMAIL_FROM=noreply@your-domain.example
RESUME_V3_PUBLIC_APP_URL=https://hr.your-domain.example

RESUME_V3_FEISHU_SMTP_HOST=smtp.feishu.cn
RESUME_V3_FEISHU_SMTP_PORT=465
RESUME_V3_FEISHU_SMTP_TLS_MODE=ssl
RESUME_V3_FEISHU_SMTP_USERNAME=noreply@your-domain.example
RESUME_V3_FEISHU_SMTP_PASSWORD=replace-with-feishu-app-password
RESUME_V3_FEISHU_SMTP_TIMEOUT_SECONDS=20
```

约束：

- `RESUME_V3_TRANSACTIONAL_EMAIL_FROM` 必须与 `RESUME_V3_FEISHU_SMTP_USERNAME` 是同一个邮箱地址。
- `RESUME_V3_PUBLIC_APP_URL` 必须是用户实际能打开的完整 HTTP/HTTPS 地址；验证链接会指向 `${PUBLIC_APP_URL}/verify-email?...`。
- SSL 模式只能使用 465；STARTTLS 模式只能使用 587。应用启动时会校验，不正确会直接拒绝启动。
- 生产 Compose 已通过 `x-app-environment` 显式把这些变量传给 `api`、`worker` 和 `migrate` 容器；只写 `.env.production` 而没有对应 Compose 映射时，容器读不到配置。

## 4. 代码结构与扩展位置

邮件抽象在 `app/services/transactional_email.py`：

- `TransactionalEmailProvider`：统一接口，目前只负责 `send_email_verification(...)`。
- `FeishuSmtpTransactionalEmailProvider`：使用标准库 `smtplib` 和 `EmailMessage` 发送纯文本 + HTML 双格式邮件。
- `TencentSesTransactionalEmailProvider`：后续切换腾讯云 SES 时复用同一调用边界。
- `build_transactional_email_provider(settings)`：按 `RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER` 选择 Provider。

配置与启动校验在 `app/config.py`。邮件发送由 `app/main.py` 的 `_deliver_email_verification(...)` 调用，发送结果只记录安全状态，不写日志记录令牌、收件人或密码。

要新增找回密码、邀请成员等邮件时，优先扩展 Provider 接口和模板构建函数；不要在业务路由中直接调用 SMTP。

## 5. 本地和测试环境

单元测试不要连接真实飞书 SMTP。使用内存 Provider：

```python
AppSettings(
    transactional_email_provider="test",
    public_app_url="http://testserver",
)
```

现有飞书 SMTP 覆盖项包括：

- SSL 465 连接、登录和 multipart 邮件构建；
- STARTTLS 587 在认证前完成 TLS；
- 注册 API 调用真实 Provider 边界的模拟链路；
- 配置不完整、端口不匹配、发件人与用户名不一致时拒绝启动；
- 邮件传输失败时仅返回稳定错误码，不记录密码、收件人或令牌；
- 配置对象的 `repr` 不显示 SMTP 专用密码。

执行：

```bash
python -m pytest -q tests/test_feishu_smtp_transactional_email.py
python -m pytest -q
cd web && npm run build
```

## 6. 生产验收步骤

1. 使用一个真实、可收信的外部邮箱注册；不要使用虚构地址。
2. 检查收件箱和垃圾邮件箱，打开验证链接。
3. 确认 `POST /v1/auth/email-verification/complete` 返回 200。
4. 确认 `GET /v1/auth/session` 中 `email_verified=true`，且可进入工作台。
5. 以未验证账号访问 `/v1/resume-library`，应被拒绝。
6. 查看 API 日志中注册与验证接口的状态码；不要在日志中打印原始邮件、令牌或密码。

注意：注册接口返回 `201` 表示账号和工作区已创建。最终验收必须以真实邮箱收到验证信、完成验证并能进入工作台为准。

## 7. 常见问题

### `邮箱+后缀@域名` 被退信

飞书邮箱不会自动把 `name+tag@domain` 当成 `name@domain`。测试时必须使用真实存在的邮箱地址；带 `+` 的别名可能被邮件服务器明确判定为收件人不存在。

### 收到 `email_delivery_not_configured`

说明应用没有可用的事务邮件 Provider。按顺序检查：

1. `.env.production` 是否设置 `RESUME_V3_TRANSACTIONAL_EMAIL_PROVIDER=feishu_smtp`；
2. Compose 是否把 SMTP 变量传给 API 容器；
3. 发件地址、SMTP 用户名和专用密码是否齐全；
4. `RESUME_V3_PUBLIC_APP_URL` 是否是完整 URL；
5. 容器重建后再检查 API 日志。

### 飞书 SMTP 连接失败或认证失败

检查 SMTP 服务是否已在飞书侧开启、使用的是否为专用密码、端口与 TLS 模式是否匹配。应用会把这类问题统一记录为 `email_delivery_provider_failed`，避免把敏感细节暴露给浏览器。

### 想改用腾讯云 SES

不需要重写注册流程。将 Provider 改为 `tencent_ses`，配置 SES 区域、模板 ID、发件地址及受控云密钥即可。业务代码继续调用相同的 `TransactionalEmailProvider` 接口。

## 8. 本次上线结论

当前生产环境已完成：飞书 SMTP 认证、注册 API、验证邮件投递记录、验证完成接口和工作区访问门禁的联调。发布时曾发现 Alembic 双 head，已通过 `20260720_0017` 合并迁移修复；后续发布应继续保持单一 migration head。

