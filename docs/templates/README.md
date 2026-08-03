# 腾讯云 SES 事务邮件模板

这两个 HTML 文件对应 GreatSell AI 的两个独立腾讯云 SES 模板：

- `tencent-ses-email-verification.html`：邮箱验证；
- `tencent-ses-password-reset.html`：找回密码。

在腾讯云控制台创建或重新提交模板时，直接复制相应文件内容。两个模板的变量都是：

- `token`：一次性令牌，只能放在固定链接的查询参数值中；
- `expires_minutes`：链接有效分钟数。

不要使用 `{{verify_url}}`、`{{reset_url}}` 或 `href="{{...}}"`。腾讯云需要在模板里看到固定的跳转域名和路径；生产配置中的 `RESUME_V3_PUBLIC_APP_URL` 必须与模板中的 `https://hr.greatsell.cn` 一致。
