# V3 云服务器部署

当前部署单元是 API、PostgreSQL、一次性迁移任务和 Caddy。Caddy 镜像会在构建阶段通过 `web/package-lock.json` 安装前端依赖、构建 React/Vite 静态文件，并在同一个 HTTPS 域名下提供页面与 API。

公网只暴露 Caddy 的 `80/443` 端口：

- `https://<domain>/v1/*` 与 `https://<domain>/health` 由 Caddy 反向代理到内部 `api` 服务；
- 其余路径由 Caddy 直接提供 `web/dist`，并回退到 `index.html`，因此刷新或直达前端路由不会 404；
- `api`、`db` 与 `migrate` 都没有宿主机端口映射。前端构建产物不包含 API 代理，API 的唯一公网入口是 Caddy 的显式路径规则。

## 首次部署

1. 在 Linux 云服务器安装 Docker Engine 和 Docker Compose 插件，并将域名 A/AAAA 记录指向服务器。
2. 将仓库部署到服务器，复制 `.env.production.example` 为 `.env.production`，填写域名、数据库密码和管理员令牌。不要把该文件提交到 Git。
3. 启动：

   ```bash
   docker compose --env-file .env.production up --build -d
   ```

   前端依赖会在 Docker 多阶段构建中通过 `npm ci` 安装；服务器检出目录不需要、也不会使用 `web/node_modules`。

4. `migrate` 容器会先运行 `alembic upgrade head`，再显式写入 985/211 院校注册表；只有它成功后 API 才会启动。
5. 验证：

   ```bash
   curl https://your-domain.example/health
   curl -I https://your-domain.example/
   ```

生产 Web 进程不会运行 `create_all()`，也不会在启动时改写院校名单。这避免多副本启动时的 DDL/种子数据竞争。

浏览器使用同源的 `/v1/*` 请求 API，不需要在生产环境设置前端 API 域名或开放 CORS。HR 主站、登录和工作台均位于 `https://hr.greatsellai.net/`，首次签发 HTTPS 证书前，只需确认该主域的 A/AAAA 记录已指向 HR 服务器，并且云防火墙放行 `80` 与 `443`。

`https://greatsellai.net/` 属于未来官网，HR 部署不得声明、接管或要求该根域指向 HR 服务器。如需保留 `https://greatsellai.net/greatsellhr/` 兼容入口，应由官网自身的边缘代理将该路径转发到 HR 主站，并继续由官网处理根路径和静态资源。

## AI 提取 worker

`compose.yml` 的 `worker` 服务负责执行已经持久化的 AI 简历提取任务；上传 API 本身不等待模型调用。共享运行环境会同时传给 API 和 worker，因此两个进程必须使用同一组模型凭据。

- 旧 DeepSeek 兼容路径使用 `.env.production` 中的 `DEEPSEEK_API_KEY`。
- 平台控制台创建的 Provider 只保存非秘密的 `credential_ref`。在 `.env.production` 中用 `RESUME_V3_AI_PROVIDER_CREDENTIALS_JSON` 提供 JSON 对象，键必须与该 `credential_ref` 完全一致，值才是实际服务端密钥。例如：`{"provider-primary":"<仅服务器保存的密钥>"}`。

该映射不会被写入数据库、审计事件、前端构建产物、请求体或应用日志。修改后需要按受控发布流程重建并重启 API 与 worker，不能只重启其中一个进程。平台控制台的“运行时凭据已配置”仅表示当前 API 进程能解析该引用，不是对上游连通性或额度的保证。

未配置所选路由的凭据时，系统不会向上游发起请求；新路由发布会被拒绝，已排队任务会显示为不可用或安全失败，直到部署负责人补齐环境映射并完成受控发布。任务状态、租约、重试上限和本地启动方式见 [AI 提取后台任务](AI_EXTRACTION_WORKER.md)。

## 受控更新、发布标签与回滚

### 代码基线

- GitHub `main` 是唯一代码基线。开发在功能分支完成、测试通过、PR 审核并合并后，才允许发布。
- 服务器目录 `/home/ubuntu/resume-screening-v3` 只是部署目标，不是 Git 协作工作区。不要直接修改其中的业务代码，也不要把它反向同步到 GitHub。
- `.env.production`、PostgreSQL 数据、Docker 卷、候选人 PDF、SSH 私钥和任何服务端密钥都不进入 Git，也不由发布脚本传输或删除。

### 创建生产版本

在本地干净的、与 `origin/main` 完全一致的 `main` 分支上运行：

```bash
scripts/create-production-tag.sh
```

脚本会创建并推送带注释的 `prod-YYYYMMDD-<commit短码>` 标签。标签已经存在时会失败；
不要移动、删除或重用生产标签。仓库管理员还应在 GitHub 设置中保护 `prod-*` 标签，
禁止强制更新和删除。

### 部署标签

从仓库根目录运行（私钥路径仅作为本地命令参数，绝不写入仓库）：

```bash
scripts/deploy-production.sh prod-YYYYMMDD-<commit短码> \
  --ssh-key /path/to/server-key
```

脚本只接受已推送到 GitHub、且可从 `origin/main` 追溯的 `prod-*` 标签。它通过
`git archive` 传输 Git 受控源码，不使用删除式同步，因此不会覆盖或删除生产环境
文件、数据库、上传 PDF 或 Docker 卷。

部署前，脚本会读取服务器项目目录外的发布记录。首次部署或发现 `migrations/`
变化时，会在 `/home/ubuntu/greatsellai-hr-deployments/backups/` 创建受限权限的
PostgreSQL 逻辑备份。发布记录位于
`/home/ubuntu/greatsellai-hr-deployments/releases/`，仅记录标签、提交号、时间和
验证状态，不记录密钥或候选人信息。

脚本重建 `api`、`worker` 和 `caddy`，让 Compose 正常执行迁移依赖，并验证：

- Caddy 配置有效；
- `https://<RESUME_V3_DOMAIN>/health` 可用；
- 未登录会话仍处于登录保护状态；
- 不携带认证信息时，伪造 ID 的原 PDF 请求被拒绝。

只有这些检查通过后，发布才会成为服务器的当前生产版本。

### 应用回滚

选择之前成功发布过的标签：

```bash
scripts/rollback-production.sh prod-YYYYMMDD-<commit短码> \
  --ssh-key /path/to/server-key
```

回滚同样会重建应用并完成健康检查，且会把回滚结果写入部署记录。它绝不从服务器
当前文件“回滚”。

如果当前版本与目标版本间包含 Alembic 迁移，脚本默认拒绝回滚。负责人必须先判断
旧应用代码是否可以读取当前数据库 schema；只有确认兼容后才能显式传入
`--allow-schema-ahead`。该模式跳过迁移并保留当前数据库，**不会**自动执行
`alembic downgrade`。如确实需要恢复数据库，必须基于部署前逻辑备份单独审批和
执行，避免覆盖新产生的业务数据与候选人资料。

### 例行备份与安全

- 定期执行 PostgreSQL 逻辑备份并演练恢复。上传的 PDF 位于 Docker volume `uploads_data`，备份时必须与数据库一起处理。
- 单账号登录口令只配置在服务端环境变量。浏览器通过同源 `/v1/auth/login` 提交口令，服务端签发 `HttpOnly`、`SameSite=Strict` 的会话 Cookie；口令绝不写入源码、`localStorage`、请求 URL 或上传内容。
- 在上线真实简历前，务必使用 HTTPS 域名并限制服务器 SSH、数据库端口和 Docker 管理权限。
