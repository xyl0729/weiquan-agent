# 维权作战 Agent：DeepSeek 咨询流水线设计

## 文档状态

- 日期：2026-08-06
- 版本：v1
- 状态：分段设计已获用户批准，待用户审阅本文档
- 项目根目录：`D:\agent development\weiquan-agent`

本文档描述本阶段咨询 Agent 的实现边界、固定流程、数据契约、错误处理和验收标准。它不修改法律数据本身，也不替代法律专业意见。

## 1. 目标与范围

项目将日常纠纷描述整理成可执行的证据清单、时效提示、沟通方案和后续行动建议。输出必须说明事实依据、规则命中情况和本地法条来源，不预测个案最终裁判结果。

本阶段包含：

- FastAPI 咨询接口和健康检查。
- 会话及追问的 SQLite 持久化，默认 TTL 为 72 小时。
- 固定 Agent Pipeline。
- 押金扣留正式 playbook，以及按已核验基准逐步扩展其他场景。
- 本地已核验法条检索和引用。
- 确定性规则求值器。
- 可审计 JSON 方案。
- Jinja2 文书和行动方案模板。
- DeepSeek 真实适配器和离线 `FakeProvider`。
- 限流、熔断、费用记录和安全错误处理。

本阶段不包含：

- Anthropic provider。
- FAISS、Embedding、LangChain 或其他向量检索依赖。
- 账号、登录、消息推送和支付系统。
- 网页界面、进度分享页和浏览器验收。
- 自动抓取或模型生成法律条文。
- 自由工具循环或由模型决定流程顺序。

已核验的召回基准包含 64 条样本、9 类场景。当前基准实际还包含 `procedure` 小额诉讼场景，因此以 `data/retrieval_benchmark.yaml` 的 9 类为验收来源，先打通押金场景的正式端到端流程，再覆盖其余已核验场景。

## 2. 不可突破的边界

1. 法条正文、法律名称、条号、数字序号、生效日期和来源链接只能来自 `data/statutes.db` 及其已核验种子文件。
2. `verdict` 必须由 playbook 的确定性规则产生。模型不能生成、修改或覆盖 `verdict`。
3. 模型只负责有限的场景分类、事实槽位抽取和非法律结论话术润色。
4. 主流程由应用代码固定编排。模型不能选择工具、跳过步骤或改变步骤顺序。
5. 每个会话最多进行两轮追问；仍缺少关键事实时返回保守的 `need_more_facts`。
6. 禁止 `eval()`、`exec()` 和等价的任意代码执行。
7. DeepSeek Key 只从本地 `.env` 或受控运行环境读取，不进入数据库、日志、响应和前端资源。
8. 用户输入作为不可信数据处理。用户不能通过输入改变模板路径、规则结构、法条引用或 SQL。

## 3. 模块边界

### 3.1 API 层

`app/api` 负责请求模型校验、会话 ID 处理、异常到 HTTP 响应的映射，以及统一响应结构。它不执行法律规则，也不直接拼接法条文字。

拟增加：

- `app/api/consult.py`
- `app/api/schemas.py`

现有的 `app/api/health.py` 保持健康检查职责。

### 3.2 Agent 编排层

`app/agent` 只负责按固定顺序调用各模块，并为每一步生成审计事件。编排层不持有模型密钥，不实现规则表达式，也不直接访问 SQL。

拟增加：

- `app/agent/pipeline.py`
- `app/agent/models.py`
- `app/agent/errors.py`

### 3.3 Provider 层

`app/providers` 对模型调用提供稳定的内部接口。真实网络实现和离线实现必须可以互换。

拟增加：

- `app/providers/base.py`
- `app/providers/deepseek.py`
- `app/providers/fake.py`
- `app/providers/factory.py`

### 3.4 Playbook 和规则层

`app/playbooks` 负责加载、校验和选择 YAML playbook。规则求值器只接受已校验的结构化事实和已加载的规则，不接受任意字符串表达式。

拟增加：

- `app/playbooks/schema.py`
- `app/playbooks/evaluator.py`
- `app/playbooks/registry.py`
- `app/playbooks/deposit_deduction.yaml`

现有的开发用 `test_scenario.yaml` 继续保留为技术检索演示，不作为正式法律咨询场景。

### 3.5 检索层

`app/retrieval` 继续使用本地 SQLite FTS5。强制法条引用必须通过 `get_statute_by_ref` 精确命中；补充检索结果只能来自同一个已核验数据库。

检索层返回不可变的法条记录对象，供规则审计和模板渲染使用。模型不能写入或修改法条库。

### 3.6 渲染层

`app/rendering` 使用 Pydantic 结构化方案作为唯一模板上下文。Jinja2 模板从固定目录加载，模板名称由 playbook 选择，不能由用户输入直接决定。

拟增加：

- `app/rendering/models.py`
- `app/rendering/renderer.py`
- `app/rendering/templates/consultation_plan.j2`
- `app/rendering/templates/evidence_request.j2`

模板只渲染经过校验的事实、规则结果、行动步骤和引用对象，不自行推导法律结论。

### 3.7 会话、限制和配置

拟增加：

- `app/db/models.py`
- `app/db/session.py`
- `app/jurisdiction/schema.py`
- `app/jurisdiction/rules.py`
- `app/limits/rate_limit.py`
- `app/limits/circuit.py`
- `app/limits/usage.py`

`app/config.py` 负责 DeepSeek 配置、数据库路径、TTL、限流和费用上限。所有相对路径都解析到项目根目录。

## 4. Provider 契约

Provider 使用异步接口，避免把 HTTP 细节泄露给 Pipeline：

```python
class LLMProvider(Protocol):
    async def extract_facts(
        self,
        message: str,
        context: dict[str, object],
    ) -> ExtractionResult:
        ...

    async def polish_text(
        self,
        draft: PolishingDraft,
    ) -> str:
        ...
```

`ExtractionResult` 至少包含：

- 受注册表允许的 `scenario_id`。
- 只属于当前 playbook 的事实槽位。
- 模型未能确认的槽位名称。
- 可选的分类置信度。
- provider、模型、请求 ID 和 token 用量等非敏感元信息。

抽取结果进入本地 Pydantic 校验和槽位合并流程后，才能被规则使用。模型返回的任何 `verdict`、法条正文、法条日期或来源字段都会被拒绝。

`polish_text` 只能接收已锁定的草稿片段，输出普通话术。它不能接收或返回可改变规则的结构化字段。润色失败时直接使用 Jinja2 原文模板。

### 4.1 DeepSeekProvider

DeepSeek 使用兼容 Chat Completions 的 HTTP 接口，由 `httpx.AsyncClient` 调用，不增加额外模型 SDK：

```ini
# 默认值为 fake；只有填写 Key 并显式切换后才访问网络
LLM_PROVIDER=fake
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=1
```

请求使用 Bearer 认证和结构化输出约束；即使 Provider 支持 JSON 输出，也必须经过本地 schema 校验。超时、网络错误、429、5xx 和非法 JSON 都转换为统一 Provider 异常，并记录脱敏的错误类别。

### 4.2 FakeProvider

`FakeProvider` 返回固定、可重复的结构化结果，不访问网络。它用于：

- 无 Key 的本地开发。
- pytest 和 CI。
- API 和 Pipeline 集成测试。
- 错误降级测试。

当 `LLM_PROVIDER=fake` 时始终使用 FakeProvider。测试也可以通过依赖注入直接传入 FakeProvider。显式选择 `deepseek` 但没有 Key 时，真实请求返回配置错误，不静默伪装成真实模型结果。

## 5. 固定 Pipeline

每次咨询严格按以下顺序执行：

1. 校验消息长度、会话 ID 和可选辖区信息。
2. 创建或恢复 72 小时内的会话。
3. 从注册表加载允许的 playbook ID、别名和槽位定义。
4. 在允许的 playbook 集合内，通过 Provider 完成场景分类和事实槽位抽取。
5. 由本地注册表校验 `scenario_id`，将新事实与会话事实合并，并校验类型和范围。
6. 检查必填槽位。缺失且追问次数未达到 2 轮时，生成 playbook 中预先声明的追问。
7. 信息足够时，从本地数据库执行强制引用检索和 FTS 补充检索。
8. 用确定性规则求值器生成规则命中记录和 `verdict`。
9. 由确定性辖区模块计算已配置的辖区提示和时效信息；不支持的辖区返回升级结果，不猜测地方标准。
10. 创建锁定的审计 JSON 草稿，确保 `evidence_now` 位于方案输出的第一部分。
11. 用 Jinja2 渲染方案和文书；必要时只对非法律事实话术调用 `polish_text`。
12. 保存 turn、审计事件和模型用量。
13. 返回统一响应。

如果步骤 3、7、8 或 9 的数据完整性检查失败，流程立即终止，不返回推测性法律结论。

## 6. API 数据契约

### 6.1 请求

`POST /api/consult`

```json
{
  "session_id": null,
  "message": "房东说要扣除全部押金，我想知道是否合法",
  "jurisdiction": null
}
```

约束：

- `session_id` 可选，格式为 UUID。
- `message` 必须非空，并限制最大长度。
- `jurisdiction` 可选；v1 只对 playbook 明确支持的辖区给出规则结果。
- API Key 不放在 JSON body，也不通过 `X-User-API-Key` 传入。DeepSeek 测试 Key 只通过 `.env` 提供。

### 6.2 领域响应

业务结果使用 HTTP 200，`status` 取以下值：

- `need_more_facts`
- `ready`
- `escalate`

```json
{
  "session_id": "uuid",
  "turn_id": "uuid",
  "followup_round": 0,
  "can_ask_more": true,
  "status": "ready",
  "verdict": {
    "code": "conservative_result",
    "label": "依据当前事实的初步判断",
    "rule_ids": ["r01"]
  },
  "plan": {
    "summary": "...",
    "evidence_now": [],
    "actions": [],
    "limitations": []
  },
  "questions": [],
  "citations": [
    {
      "ref": "住房租赁条例.第十条",
      "law_name": "...",
      "article_no": "...",
      "content": "...",
      "effective_date": "YYYY-MM-DD",
      "source_url": "https://..."
    }
  ],
  "audit_id": "uuid"
}
```

`need_more_facts` 时 `questions` 非空，`plan` 和 `citations` 可以为空。达到两轮追问上限后仍缺信息时，继续使用 `need_more_facts`，将 `can_ask_more` 设为 `false`，并在限制字段中说明不能作确定判断。

基础错误映射：

- `422`：请求格式或字段校验失败。
- `429`：客户端限流或日配额耗尽。
- `503`：Provider、数据库或熔断器暂不可用。
- `500`：内部完整性错误。响应不包含堆栈、密钥或原始 Prompt。

## 7. Playbook 规则设计

正式 playbook 必须包含版本、场景 ID、槽位定义、法条引用、规则分支、默认结果、证据清单和升级条件。

规则条件使用有限节点，而不是任意字符串表达式。允许的比较操作为 `exists`、`eq`、`neq`、`in`、`gte`、`lte` 和 `contains`；允许的组合节点为 `all`、`any` 和 `not`。规则只能读取当前 playbook 已声明的槽位、默认值和受支持的辖区字段。v1 不使用 `simpleeval`，也不把 playbook 文本交给 Python 表达式解释器。

规则结果使用注册表中的 verdict code。每个正式 playbook 必须声明其允许的 code，加载时拒绝未知 code。规则执行结果至少记录：

```json
{
  "rule_id": "r01",
  "matched": true,
  "inputs": ["deposit_amount", "withheld_amount"],
  "verdict": "need_more_facts",
  "key_point": "..."
}
```

可选槽位必须声明默认值；规则求值前先补齐默认值，避免用未定义字段参与判断。`default` 分支必须存在，且只能产生保守结果或升级结果。

押金场景的槽位名称围绕以下事实设计，具体规则只在对应法条和核验材料支持时启用：

- 合同中的押金和扣除约定。
- 退租和交房时间。
- 扣除理由及金额。
- 房屋或物品损坏描述。
- 正常损耗与具体损坏的区分事实。
- 维修、验收、照片、收据等证据。
- 房东通知和协商记录。
- 所在辖区及争议处理状态。

规则结果必须是注册枚举，不允许 YAML 任意写入新的 verdict。每条分支都应有唯一规则 ID，默认分支必须是保守结果。

辖区和时效配置与法律正文分离。只有经过本地核验并登记的辖区字段才可参与求值；没有对应配置时返回 `escalate`，不根据模型知识补全。用户输入的日期只作为事实进入确定性日期计算，法定期间的类型和起算字段必须来自 playbook 配置。

## 8. 会话和审计模型

`sessions` 保存：

- `id`
- `scenario_id`
- `followup_round`
- `status`
- `created_at`
- `updated_at`
- `expires_at`

`turns` 保存：

- `id`
- `session_id`
- 用户消息正文，仅在会话 TTL 内保存在本地数据库，不写入应用日志。
- 结构化事实快照。
- 规则命中快照。
- 响应 JSON。
- Provider 名称、模型名称、token 用量和费用估算。
- 创建时间。

`audit_records` 保存：

- `audit_id`
- 每个 Pipeline 阶段的开始、结束和状态。
- 使用的 playbook 版本。
- 检索到的法条引用。
- 错误类别和降级路径。

审计记录不保存 API Key、Authorization Header 或完整的 Provider 请求头。日志只记录会话、turn、audit 和错误类别 ID。

## 9. 错误处理与安全

Provider 抽取失败时最多固定重试一次。再次失败后：

- 没有足够的已验证事实：返回 `503 provider_unavailable`。
- 已有足够的已验证事实：跳过润色，继续本地规则和模板渲染。

法条数据库缺失、metadata 门禁失败、强制引用找不到或 playbook schema 无效时，应用 fail closed，不输出法律结论。

模板和规则文件只能从项目内固定目录读取。所有外部输入都经过长度、类型、枚举和结构校验。限流、熔断和日费用额度在调用真实 Provider 前检查，并在调用后记录用量。

费用计算只使用显式配置的 DeepSeek 输入和输出单价；未配置单价时记录 token 用量但不伪造费用。默认测试路径不访问网络。

## 10. 测试设计与验收

### 10.1 单元测试

- Provider 工厂在 `fake`、`deepseek`、缺 Key 和测试注入场景下的选择。
- DeepSeek 请求格式、超时、429、5xx 和非法 JSON。
- FakeProvider 的确定性输出。
- playbook schema、引用校验和规则操作。
- 槽位合并、追问次数和会话 TTL。
- 模板上下文边界和响应 schema。
- 限流、熔断和费用累计。

### 10.2 集成测试

- `POST /api/consult` 的首次咨询、追问、会话恢复和最终方案。
- 完整押金场景从抽取到审计 JSON、法条引用和模板文书。
- Provider 不可用时的安全降级。
- 过期会话、非法 session ID、过长输入和未知场景。

### 10.3 数据回归测试

- 64 条召回样本全部执行。
- `Recall@5` 保持 `1.000`。
- `scripts/verify_refs.py` 通过。
- 所有正式 playbook 引用精确命中本地法条。
- 两个数据文件和生成数据库的 `placeholder_data` 均为 `false`。

### 10.4 DeepSeek 联调

真实联调是显式开启的本地测试，不进入默认 pytest 或 CI。联调前用户只需在项目根目录 `.env` 填写：

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-chat
```

测试输出只展示状态、模型名称、耗时和脱敏用量，不展示密钥。Key 不应提交到 Git。

## 11. 实现顺序

1. Provider 配置、接口、FakeProvider 和 DeepSeek HTTP 适配器。
2. 会话数据库、审计模型和统一 API schema。
3. playbook schema、注册表和确定性规则求值器。
4. 押金正式 playbook 与 Jinja2 渲染。
5. Pipeline 串联、追问和错误降级。
6. 限流、熔断、费用记录和健康检查扩展。
7. 其余已核验场景逐个迁移并补充分支测试。
8. 全量回归和可选的 DeepSeek 本地联调。

完成上述步骤后，再单独评估 Web 界面和进度分享页，不将其混入本阶段实现。
