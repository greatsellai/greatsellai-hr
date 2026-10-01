# API 与 MCP 外部连接

## 目标与上线状态

大卖智聘通过 REST API 提供有边界的数据读取能力，并通过 MCP 提供面向 WorkBuddy、Codex 等 AI 工作区的工具入口。两者共用相同的用户授权、工作区隔离、权限范围、限流、审计和数据投影，不复制简历库。

当前分支包含首版实现与部署配置，尚未完成迁移验收、PR 合并或生产发布。所有 `RESUME_V3_INTEGRATIONS_*` 功能开关默认关闭；代码存在不代表客户线上已可使用。

## 客户端入口与授权

- 外部 API：`https://hr.greatsellai.cn/v1/integrations`
- MCP Streamable HTTP：`https://hr.greatsellai.cn/v1/mcp`
- OAuth 元数据：`https://hr.greatsellai.cn/.well-known/oauth-authorization-server`
- MCP 受保护资源元数据：`https://hr.greatsellai.cn/.well-known/oauth-protected-resource/v1/mcp`
- 系统内设置入口：工作区设置中的“API 与 AI 工具连接”。

API 密钥与 OAuth 授权均绑定到完成登录的用户和当前工作区。API 密钥只在创建或轮换时展示一次；系统只保存摘要。OAuth 使用授权码 + PKCE（S256），访问令牌短期有效，刷新令牌轮换。用户可随时撤销授权。不得将密钥写入 URL、源码、聊天记录或客户端共享配置。

OAuth 连接另有防滥用容量保护：每个用户账号跨工作区最多保留 20 个不同客户端授权，每个工作区最多 200 个；同一客户端在多个工作区复用时只占该用户一个名额，但仍计入每个工作区的独立上限。达到上限时，授权会返回 `oauth_connection_limit_reached`，并可稍后重试。过期或撤销的授权保留 30 天恢复期，其间仍占容量且客户端 ID 保持可用；恢复期结束后，后台可回收令牌族及不再被其他授权使用的客户端 ID，不额外延长 30 天。该上限是服务安全容量限制，不改变套餐、价格或 API 数据额度。

开通授权前，工作区管理员须启用工作区策略并选择允许的权限；集成专用权限不能扩大用户在网页端原有的数据权限。设置页显示用户、企业、授权范围和外部 AI 数据披露。客户应将允许读取的资料视为会发送给所选第三方 AI 服务；撤销无法召回此前已传出的内容。

## 首版接口与 MCP 工具

| 功能 | REST | MCP 工具 | 权限范围 |
|---|---|---|---|
| 连接身份 | `GET /connection` | `get_connection_info` | 有效连接授权（不读取候选人资料） |
| 筛选条件 | `GET /filter-options` | `get_filter_options` | `candidates:read` |
| 搜索候选人 | `POST /candidates/search` | `search_candidates` | `candidates:read` |
| 候选人结构化资料 | `GET /candidates/{id}` | `get_candidate_profile` | `candidates:read` |
| 原文证据片段 | `POST /candidates/{id}/evidence` | `get_candidate_evidence` | `evidence:read` |
| 既有总结、评分与匹配 | `GET /candidates/{id}/assessments` | `get_candidate_assessments` | `assessments:read` |
| 岗位列表与版本要求 | `GET /jobs`、`GET /jobs/{id}/versions/{version_id}` | `list_jobs`、`get_job_requirements` | `jobs:read` |
| 准备本人分析草稿 | `POST /analysis-reports` | `prepare_analysis_draft` | `analyses:write` |
| 查询本人草稿 | `GET /analysis-reports`、`GET /analysis-reports/{id}` | `list_analysis_drafts`、`get_analysis_draft` | `analyses:read` |

上述 REST 路径均相对于 API 根路径。API 默认权限仅包含候选人、岗位和既有评估只读范围；证据读取和分析草稿读写须单独授权。查询每页最多 100 人。访问量受每授权、每工作区请求数、并发数和每日去重候选人数限制，API/MCP 共用额度；创建新密钥不绕过工作区额度。

## 数据边界

- 每次请求重新检查账号状态、工作区成员资格、网页端当前权限、令牌、工作区策略和资料有效状态；调用方不能传入组织 ID 切换租户。
- 候选人默认以系统代号展示；结构化资料投影不包含姓名、联系方式或证件字段，也不提供原始文件下载、完整简历文本或原文件路径。
- 证据读取为单独授权且默认关闭。系统会对片段应用常见联系字段、模式和已知候选人姓名的规则化清理，但这不是可靠匿名化保证：未标注的人名、机构线索或其他自由文本个人信息可能无法识别。启用 `evidence:read` 前，应将任何返回片段都视为可能含个人信息，并确认本组织允许将其发送到所选外部 AI 服务；撤销不能追回此前已发送的数据。
- 分析草稿与系统正式评分分开呈现；外部工具先提交 15 分钟待确认项，它不会出现在外部草稿列表或详情中。用户必须登录网页，在“API 与 AI 工具连接”的待确认区核对来源、候选人、简历事实与分析内容后明确确认，才成为仅创建者可见的私有草稿。MCP 工具名为 `prepare_analysis_draft`；模型不能通过请求字段代替用户确认。草稿不自动写入招聘状态，不代表录用或淘汰结论。
- 草稿只归创建者查看。草稿保存并固定关联当时的候选人事实、简历与岗位版本；候选人或被引用简历删除时，关联草稿会失效并按保留流程清理。
- 简历中的指令性内容属于不可信数据，不能改变工具权限、扩大数据范围或触发候选人状态变更。
- AI 仅提供招聘辅助依据，最终判断由招聘团队作出；系统不自动拒绝或录用候选人。

## 配置与发布安全

生产和 staging Compose 将 API、MCP、分析草稿及 OAuth 开关分别默认置为 `0`。不得仅为验证而直接打开生产开关。首次启用前必须完成：

1. 在目标数据库验证 Alembic 迁移来源、当前 revision、升级后端业务回归和回滚兼容性。
2. 在隔离环境用合成数据验证 REST、MCP、OAuth 发现、授权、撤销和私有草稿。
3. 确认 Caddy 只反向代理两个精确 OAuth/MCP discovery 路径；不得配置通配 `/.well-known/*`。
4. 对指定试点工作区启用；完成客户端实际调用和隔离测试后，再按发布流程扩大范围。

增加新数据库表不代表旧程序自动兼容业务行为。尤其是分析草稿引用候选人和简历；若回滚到不了解这些引用的旧版本，必须先确保集成关闭且无引用记录，否则旧清理流程可能无法安全删除候选人资料。应用回滚不得自动降级数据库。

当前仓库规定 PR 完整验证、合并和受控 staging/production 发布。跳过 CI 不等于可以手工 SSH 绕过发布门禁；如需线上发布，先按项目发布流程确认资格、迁移版本、配对备份、回滚标签和运行环境，再由获批通道执行。
