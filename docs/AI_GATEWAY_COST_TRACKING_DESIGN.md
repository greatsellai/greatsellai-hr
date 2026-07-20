# AI 模型网关与 API 成本追溯设计

**状态：** D0 已完成；D1/D2 与 D3 的后端控制面、账本查询已实现，待 PR 审核；OCR 适配、连接测试、预算与前端平台控制台仍未实现
**日期：** 2026-07-20
**范围：** 模型无关的调用网关、平台管理员模型策略、LLM/OCR 成本追溯与预算基础设施
**不包含：** 生产部署、供应商接入、客户自带 Key、用户自选模型或自动最低价路由

## 1. 决策摘要

1. GreatSell 的业务代码不得绑定任何具体模型、供应商、URL、Key 名称或供应商专属参数。
2. 只有平台管理员可以选择、发布、回滚模型策略；工作区用户和招聘人员不看到模型选择器。
3. 每个业务功能只引用稳定的 `feature`，运行时由已发布的策略版本解析实际模型档案。
4. 自动回退默认关闭。只有平台管理员明确配置过的备用目标，且错误类别符合策略时，才允许回退。
5. 每一次实际对外 API 请求均有一条不可变调用账本；一次业务动作中的重试、结构修复和回退必须分别入账。
6. 成本优先采用厂商响应中的实际用量；无法取得时才估算，并明确标记估算、未知或“可能已计费”。
7. 单价、Prompt 版本、模型档案和路由策略均按调用冻结。后续改价、切模型或改 Prompt 不得改写历史成本和历史结论。
8. 调用账本不保存候选人原文、Prompt、模型原始回复、Authorization、API Key 或厂商原始错误体。

## 2. 目标与非目标

### 2.1 目标

- 在不修改简历、评分、JD 匹配和 Agent 业务规则的前提下，可由平台管理员切换任一已注册模型。
- 将简历提取、评分、总结、JD 生成、JD 要求提取、JD 匹配、Agent 和 OCR 的外部 API 成本关联到正确的工作区和业务动作。
- 清楚区分一次用户动作、一次后台任务、一次供应商请求、缓存复用、重试和跨供应商回退。
- 为后续的额度、预检、批量任务成本估算、模型质量 A/B 对比和收费能力提供稳定底座。
- 保留现有“事实快照 + 本地 Schema + 证据 ID 校验”机制；换模型不能降低事实可靠性。

### 2.2 明确不做

- 不将模型名称或供应商选择暴露给普通用户、工作区管理员或浏览器。
- 不建立绕过订阅限额的账号池，不接入个人 Coding Plan 作为生产供应商。
- 不按最低单价自动挑选模型，也不让系统私自改变模型。
- 不把真实简历 Prompt 或模型回复发送到外部观测 SaaS。
- 不试图精确补齐历史调用成本。历史记录缺少 Token、请求 ID 和响应 usage，只能保留现有结果，不回填为“精确成本”。
- 一期不做客户自带 Key（BYOK）、供应商 Key 轮换池、动态负载均衡或跨工作区差异化模型策略。

## 3. 当前基线与问题

当前代码能完成 AI 业务闭环，但模型调用尚未成为独立基础设施：

- `app/services/deepseek_provider.py` 同时包含 Prompt、领域 Schema、证据校验、HTTP 请求、供应商错误码和供应商专属参数。
- `app/services/recruiting_agent_service.py` 绕开上述模块，单独向模型发 HTTP 请求。
- 简历提取、评分、总结、JD 生成、JD 要求提取、JD 匹配以及批处理服务均直接读取当前供应商配置。
- OCR 通过独立的腾讯 OCR 调用执行，也没有页数、请求 ID 或成本账本。
- 结果表只在部分结果中保存 `model_name`；没有统一的 provider、Token、缓存 Token、请求 ID、单价、成本、重试链或路由版本。

因此，当前无法可靠回答“哪个工作区、哪个功能、哪次任务、哪个模型花了多少钱”，也无法在不触碰业务代码的情况下切换模型。

## 4. 总体架构

```text
简历提取 / 评分 / 总结 / JD / Agent / OCR
                  │
                  │ feature + 业务引用 + 工作区上下文
                  ▼
             AI Gateway（唯一外呼入口）
       ├─ 运行与预算控制
       ├─ 路由策略解析与能力校验
       ├─ 重试 / 显式回退
       ├─ 本地输出校验前后的账本收口
       └─ 成本计算与审计
                  │
                  ▼
          Provider Adapter（协议适配层）
       ├─ OpenAI Compatible Adapter
       ├─ 原生协议 Adapter
       ├─ OCR Adapter
       └─ 后续异步 Batch Adapter
                  │
                  ▼
                 正式 API
```

领域层保留 Prompt、领域 Schema、事实校验、证据校验和业务写库逻辑。Adapter 只处理厂商协议差异；Gateway 只处理运行控制、路由、调用账本和成本。

## 5. 核心概念与权限边界

### 5.1 平台级控制面

以下对象属于平台控制面，不带 `organization_id`，仅平台管理员可读写：

- `provider_profile`：一个供应商连接配置，包含驱动类型、API 基址、密钥引用和可用状态。
- `model_profile`：一个可被选择的模型档案，包含真实模型 ID、能力、上下文限制、数据处理属性和启用状态。
- `model_price_version`：模型档案在一个生效时刻的计费规则。只新增，不更新历史版本。
- `route_policy`：某个业务 `feature` 的策略容器。
- `route_policy_version`：一次已发布的、不可变的模型选择与重试/回退规则。
- `prompt_revision`：某个业务 Prompt/Schema 的不可变版本引用。

平台管理员通过内部模型控制台发布策略。工作区用户无权查看供应商、模型、Key、单价或策略编辑入口。

### 5.2 工作区级运行面

以下对象必须带 `organization_id`，并纳入现有 tenant scope：

- `ai_run`：一次业务层 AI/OCR 操作，例如“某简历事实提取”或“一次 Agent 对话”。
- `api_invocation`：一次真实向外部服务发出的请求。一次 `ai_run` 可对应零到多次 `api_invocation`。
- 后续预算、配额、预检和日聚合记录。

`organization_id` 不能仅通过关联资源间接推断；每个运行根记录和队列根记录均必须直接保存它，以防 worker 或按 ID 查询时越权。

## 6. 模型档案与策略发布

### 6.1 Provider Profile

`ai_provider_profiles` 的建议字段：

```text
id, slug, driver, base_url, credential_ref,
request_defaults_json, enabled, created_at, updated_at
```

- `driver` 是协议类型，例如 `openai_compatible`、`anthropic_native`、`ocr_native`、`batch_native`，不是某个模型名。
- `credential_ref` 只保存服务端密钥标识，例如环境变量或密钥管理系统中的引用；不保存明文 Key。
- 修改连接信息应创建新的 Profile 或显式版本，不覆盖已产生调用账本的历史含义。

### 6.2 Model Profile

`ai_model_profiles` 的建议字段：

```text
id, provider_profile_id, slug, provider_model_id,
capabilities_json, context_window, max_output_tokens,
data_classification_json, enabled, created_at, retired_at
```

`capabilities_json` 至少表达：

```text
strict_json_schema
forced_tool_choice
json_object
tool_calling
vision
streaming
batch
supports_system_message
supports_thinking_toggle
```

业务功能声明自己需要的能力，而不写供应商判断：

- 简历事实提取、评分、JD 要求提取和 JD 匹配：需要严格结构化输出或强制工具调用。
- 招聘 Agent：需要工具调用。
- 图片/扫描件直传模型（未来）：需要视觉；当前优先 OCR 后文本处理。
- 历史异步批处理：需要 Batch；不能作为同步接口的临时回退。

### 6.3 Route Policy 与版本

`ai_route_policies` 是稳定功能名到当前版本的指针：

```text
id, feature, active_version_id, enabled, created_at, updated_at
```

`ai_route_policy_versions` 是不可变策略快照：

```text
id, policy_id, version, status, targets_json,
retry_policy_json, max_cost_guard_json, published_by_user_id,
published_at, supersedes_version_id
```

`targets_json` 是管理员明确发布的有序目标，例如：

```text
[
  {
    "model_profile_id": "…",
    "timeout_seconds": 90,
    "max_output_tokens": 5000,
    "allow_fallback_on": ["timeout", "rate_limited", "provider_5xx"]
  }
]
```

没有第二个目标就没有回退。系统不得根据价格、模型名称或供应商名称自行添加备用目标。

### 6.4 策略生效规则

- 新的同步请求读取当前已发布版本。
- 批量任务、异步提取任务、Agent 一次对话在创建时固定 `route_policy_version_id` 和 `prompt_revision_id`。
- 策略切换只影响后续新任务；不会让正在执行的一批任务前后使用不同模型。
- 已产生的评分、总结、JD 匹配继续保留原模型档案与策略版本。切换模型不自动触发全量重算；由平台管理员或业务用户显式新建重跑任务。
- 发布前必须进行连接测试和相应能力测试；测试不会写候选人数据。

## 7. 统一调用合同

### 7.1 业务请求

业务层通过一个不含供应商信息的请求调用 Gateway。概念模型如下：

```python
CompletionRequest(
    feature="resume_extract",
    organization_id=organization_id,
    actor_user_id=actor_user_id,
    run_id=run_id,
    business_ref_type="resume_ai_extraction_job",
    business_ref_id=job_id,
    prompt_revision_id=prompt_revision_id,
    contract_version="resume_facts.v2",
    messages=messages,
    tools=tools,
    required_capabilities={"forced_tool_choice"},
    max_output_tokens=5000,
)
```

`messages` 和 `tools` 在进程内传递给 Adapter，但不得写入调用账本或普通日志。请求对象不允许携带 API Key、Provider URL、模型名或浏览器传入的路由偏好。

### 7.2 统一响应

Adapter 返回统一响应：

```python
CompletionResult(
    content=...,
    tool_calls=...,
    finish_reason=...,
    provider_request_id=...,
    usage=NormalizedUsage(...),
    raw_status_code=...,
    model_id=...,
)
```

`NormalizedUsage` 使用互斥计量桶，至少支持：

```text
input_tokens
cached_read_input_tokens
cached_write_input_tokens
output_tokens
reasoning_tokens
image_units
page_units
request_units
```

若厂商把缓存 Token 包含在总输入 Token 内，Adapter 必须在归一化阶段扣除，避免账本重复计费。

### 7.3 统一错误分类

Provider Adapter 只向 Gateway 抛出标准错误类别：

```text
auth
invalid_request
rate_limited
quota_exhausted
timeout
network
provider_5xx
truncated
structured_invalid
policy_blocked
unsupported_capability
```

向前端保留稳定、供应商无关的错误码，例如 `ai_service_unavailable`、`ai_quota_exhausted`、`ai_output_invalid`。迁移期间可兼容现有错误码，但新业务不得继续新增供应商前缀的公共错误码。

## 8. 执行、重试与回退

一次结构化请求的执行顺序：

1. 业务服务创建或复用 `ai_run`，传入当前工作区与业务引用。
2. Gateway 读取该 Run 固定的路由版本；同步新请求才解析当前已发布策略。
3. 检查所选目标的能力、数据处理约束、单次成本上限和后续预算预留。
4. 在发出网络请求前写入一条 `api_invocation(status=started)`。
5. Adapter 发送请求、解析厂商响应并返回标准响应或标准错误。
6. Gateway 写入 Token、延迟、厂商请求 ID、价格快照和成本；无论成功、失败或超时均必须收尾。
7. 领域层对输出执行本地 JSON Schema、Pydantic、事实证据和业务约束校验。
8. 仅在本地校验通过后，才写入简历事实、评分、总结、JD 要求或 Agent 最终答复。

规则如下：

- 传输类错误（明确配置的 `timeout`、`network`、`rate_limited`、`quota_exhausted`、`provider_5xx`）可以按策略进入下一目标。
- `auth`、`invalid_request`、`policy_blocked` 和 `unsupported_capability` 不回退，避免把配置错误放大为额外成本。
- `structured_invalid` 或证据校验失败不跨模型盲目重试。若该功能允许，可对同一目标发起一次“结构修复”请求；该修复请求是新的 `api_invocation`。
- 每个 Run 的总目标数、修复次数和最大费用都由已发布策略限制。默认不允许无限重试。
- 对于网络超时，账本标记 `may_have_billed=true`，不能把费用写成零。

现有 worker 的 lease、幂等、事实快照锁和结果复用逻辑继续保留；Gateway 不替代这些业务层保护。

## 9. API 成本账本

### 9.1 `ai_runs`：业务动作根

建议字段：

```text
id, organization_id, actor_user_id,
feature, service_kind,
business_ref_type, business_ref_id,
parent_run_id, correlation_id,
route_policy_version_id, prompt_revision_id, contract_version,
source_snapshot_hmac, input_size_bytes,
status, started_at, finished_at,
total_cost_reporting_micros, reporting_currency
```

示例：

- 一份简历的事实提取是一个 Run；rich 提取失败后走 core 提取仍属于同一 Run。
- 一次 Agent 用户消息是一个 Run；每轮模型调用和每次工具调用关联同一 `correlation_id`。
- 一次 OCR 页面请求可以是 `service_kind=ocr` 的 Run，也可作为简历解析 Run 的子 Run。

### 9.2 `api_invocations`：一次真实外呼

建议字段：

```text
id, ai_run_id, organization_id,
attempt_no, target_index, fallback_of_id,
provider_profile_id, model_profile_id, provider_driver, provider_model_id,
provider_request_id, http_status,
status, error_category, may_have_billed,
started_at, completed_at, latency_ms,
input_tokens, cached_read_input_tokens, cached_write_input_tokens,
output_tokens, reasoning_tokens, image_units, page_units, request_units,
usage_source, usage_details_json,
price_version_id, price_snapshot_json,
provider_reported_cost_micros, calculated_cost_provider_micros,
provider_currency, reporting_cost_micros, reporting_currency,
fx_snapshot, cost_source
```

字段约束：

- 每条真实网络请求一行。首次请求、结构修复重试、同供应商重试和备用模型请求都分别写行。
- `usage_details_json` 只保存脱敏的计量桶，不保存消息、工具参数、简历内容或原始响应。
- `provider_request_id` 仅在厂商响应明确提供时记录。
- `price_snapshot_json` 复制该调用实际使用的各计费单价和单位；不依赖未来可变的价格表重算历史。
- 金额使用整数微单位保存，避免浮点误差。原始供应商币种与产品报表币种分别存储；发生换汇时冻结 `fx_snapshot`。
- 没有 usage 的成功响应使用 `usage_source=estimated` 或 `unavailable`，不得伪装为零 Token。

### 9.3 模型价格版本

`ai_model_price_versions` 建议字段：

```text
id, model_profile_id, version, currency, effective_from,
price_rules_json, source_reference, created_by_user_id, created_at
```

`price_rules_json` 支持输入、输出、缓存读、缓存写、推理、图片、OCR 页、请求次数等单位。价格版本只新增；误录价格通过新增修正版本解决，历史 invocation 不被覆盖。

### 9.4 成本来源优先级

1. 厂商返回的实际成本。
2. 厂商返回 usage × 本次冻结单价。
3. 本地估算 usage × 本次冻结单价。
4. 无法获得 usage 时标记成本未知。

成本看板必须区分“已确认”“按厂商 usage 计算”“估算”“未知/可能已计费”，不能把所有数据混成看似精确的一条数字。

### 9.5 缓存与复用

- 业务结果缓存命中时，不创建外部 `api_invocation`；`ai_run` 标记 `cache_hit`，成本为零。
- 评分、JD 匹配、总结和事实提取的现有版本复用规则不改变。
- 成本报表同时提供“实际调用成本”和“缓存复用次数”，但不虚构“节省金额”。

## 10. Prompt、事实与结果版本

模型可换不等于 Prompt 和输出标准可随意漂移。每次外呼都必须固定：

```text
resume/JD/评分模板事实版本
Prompt revision
领域 Schema/contract version
route policy version
resolved model profile
```

建议新增 `ai_prompt_revisions`，只存：

```text
id, feature, revision, template_hash,
contract_version, source_commit, published_by_user_id, published_at
```

Prompt 模板正文仍随代码或受控发布流程管理；账本只记录不可逆哈希和版本号。模型、Prompt 或合同变化后，旧结果保持历史可查；新结果必须显式重跑生成。

## 11. 预算、预检与成本控制

成本账本先于客户计费上线。预算控制在账本稳定后启用：

- 平台可为工作区设置月度/每日/单次 Run 的最大成本与调用数。
- 发起批量评分或 JD 匹配时，按未命中缓存的项目数和策略最大输出做预估；前端展示预计新增调用量和上限，而不是承诺精确金额。
- 调用前预留最大可能成本；完成后按实际成本结算并释放未使用预留，避免并发任务突破预算。
- 到达 80% 发送预警，到达硬上限不再创建新增外呼；查看历史结果和筛选不受影响。
- 重试和回退计入平台实际成本，但面向用户的计费规则可在后续单独定义，不能直接等同于供应商账单。

一期不开放成本控制面给普通工作区用户。平台管理员可查看按工作区、功能、模型档案、日期和状态聚合的内部报表。

## 12. 管理界面与 API 边界

内部平台管理界面提供：

- Provider Profile 列表与连接测试；只显示密钥引用是否已配置，不显示密钥。
- Model Profile 列表、能力、上下文限制、价格版本和启用状态。
- 功能策略草稿、发布、回滚、发布历史与当前生效版本。
- 每个功能的调用量、成功率、结构校验失败率、平均延迟和成本对比。
- 按工作区的成本与额度内部报表。

建议 API 命名：

```text
GET  /v1/platform/ai/providers
POST /v1/platform/ai/providers/{id}/test
GET  /v1/platform/ai/models
POST /v1/platform/ai/route-policies/{feature}/versions
POST /v1/platform/ai/route-policies/{feature}/publish
POST /v1/platform/ai/route-policies/{feature}/rollback
GET  /v1/platform/ai/usage
GET  /v1/platform/ai/invocations
```

这些 API 仅允许现有 `is_platform_admin` 身份访问。工作区 API 不返回 Provider、模型档案、单价、Key、原始 Prompt 或原始响应。

## 13. 现有业务的迁移映射

以下功能必须逐步迁移到 Gateway：

```text
resume_extract_rich
resume_extract_core
candidate_name_backfill
resume_score
resume_summary
jd_generate
jd_requirements_extract
jd_match
recruiting_agent_turn
resume_ocr_page
```

迁移时的原则：

- 现有 `deepseek_provider.py` 中的事实快照白名单、Schema、Prompt 构建和本地验证函数先保留，逐步移动到领域合同模块。
- HTTP 请求、认证、供应商响应解析、usage 解析和错误映射移入 Provider Adapter。
- `recruiting_agent_service.py` 不再自行构造 HTTP 请求；每轮 Agent 模型调用经 Gateway，因此 Agent 的多轮工具调用可完整追踪成本。
- 原有结果表中的 `model_name` 暂时保留以兼容 API；新结果额外关联 resolved model profile、route policy version 和对应 Run。
- 原有前端错误提示逐步切换到供应商无关错误码，确保 UI 不再出现某个供应商名称。

## 14. 分阶段实施

### 阶段 D0：设计确认

- 审核本文档与数据字段。
- 确认只有平台管理员切模型、默认无回退、成本账本包含 LLM 与 OCR。
- 不改生产、不开启新模型。

### 阶段 D1：底座与账本

- [已实现] 新增数据迁移、模型/策略/价格/Run/Invocation 表。
- [已实现] 实现统一合同、成本计算器和 OpenAI Compatible Adapter。
- [已实现] 从现有服务端密钥配置创建受控的初始 Provider/Model Profile；不修改生产环境文件。
- [已实现] 简历事实提取接入账本；[待实现] OCR Adapter、OCR 页数与成本账本。

### 阶段 D2：业务迁移

- [已实现] 迁移评分、总结、JD 生成、JD 要求提取和 JD 匹配。
- [已实现] 迁移相关批处理 worker；批次创建时锁定策略版本。
- [已实现] 迁移 Agent 的独立直连，按 Agent Run 记录每一轮模型调用和工具链。

### 阶段 D3：平台控制台与使用报表

- [已实现] 平台管理员模型档案、价格档案、策略发布和成本查询 API。
- [已实现] 工作区/功能/模型档案/日期筛选和安全调用明细 API。
- [待实现] 路由回滚 API、连接测试与前端平台控制台。
- [已实现] 普通用户无模型选择或平台控制面权限。

### 阶段 D4：第二协议与质量验证

- 接入第二个已授权正式 API 的 Adapter，但默认不进入生产策略。
- 使用脱敏或已授权测试样本比较结构有效率、证据一致性、延迟和成本。
- 只有通过该功能的 Schema 和证据测试后，平台管理员才可将其发布到某个功能策略。

### 阶段 D5：预算与异步 Batch

- 实现预算预留、批量预检、额度告警和硬上限。
- Batch 作为独立异步 Adapter，具有提交、轮询、回填和部分失败处理；不混入同步 `complete()` 回退链。

## 15. 测试与验收

### 15.1 Adapter 合同测试

- 请求格式转换、工具调用、JSON Schema、usage 解析、厂商请求 ID 和错误分类。
- 供应商专属参数不得泄漏到其他 Adapter。
- 无 usage、缓存 usage、推理 Token、OCR 页数和异常响应均可规范化。

### 15.2 Gateway 测试

- 无可用策略、能力不匹配、连接未配置均拒绝请求且不写业务结果。
- 每一次实际请求都有一条调用账本；成功、429、超时、5xx、结构修复和回退均可关联同一 Run。
- 只有策略允许的错误才能回退；鉴权、请求参数和事实校验失败不得跨模型。
- 回退次数和总成本上限有效；同一业务结果不会因重试重复写入。

### 15.3 成本与隐私测试

- 厂商 usage 优先于估算；金额使用冻结的价格快照。
- 改价后历史调用成本不变化。
- 超时显示“可能已计费”，而非零成本。
- 数据库、日志、异常 API 响应和前端网络包中不出现 API Key、Authorization、原始简历文本、Prompt 或模型原文输出。
- 两个工作区不能通过 Run、Invocation、报表或资源 ID 读取彼此数据。

### 15.4 业务回归

- 上传/提取、OCR 回退、评分、总结、JD 原版发布、AI-JD、JD 匹配、批量任务、邮箱入库和 Agent 全链路保持现有功能语义。
- 事实/证据校验失败仍不得写入可筛选事实。
- AI 结论保持辅助性质，不产生自动拒绝、自动录用或自动外发行为。

## 16. 参考实现与取舍

- LiteLLM：借鉴“模型别名/真实部署分离、Router、不可变 Spend Log、预算预检”的思想；不部署其 Proxy、虚拟 Key、Redis 或重复的组织体系。
- Portkey：借鉴“配置化路由策略、显式回退链、Config/Trace 关联、Provider/Model Catalog 分层”；不开放任意配置注入，也不把简历请求经过额外第三方网关。
- Langfuse：借鉴“业务 Run/Trace 与单次 Generation 分离、Prompt 版本、厂商 usage 优先、价格在写入时冻结”的数据模型；不把原始输入输出发送到云端。
- Helicone：借鉴“模型/端点/价格规则分层、不同计费单位、按业务标签归因”的成本模型；不启用自动最低价路由。

官方参考：

- [LiteLLM Router](https://github.com/BerriAI/litellm/blob/main/litellm/router.py)
- [LiteLLM Spend Tracking](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/spend_tracking/spend_tracking_utils.py)
- [Portkey Configs](https://portkey.ai/docs/product/ai-gateway/configs)
- [Portkey Model Catalog](https://portkey.ai/docs/product/model-catalog)
- [Langfuse Token & Cost Tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking)
- [Langfuse Data Model](https://langfuse.com/docs/observability/data-model)
- [Helicone Cost Registry](https://github.com/Helicone/helicone/tree/main/packages/cost)
