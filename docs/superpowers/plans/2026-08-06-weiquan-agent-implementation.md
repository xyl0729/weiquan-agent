# 维权作战 Agent 实现计划

> 日期：2026-08-06
> 依据：[DeepSeek 咨询流水线设计](../specs/2026-08-06-weiquan-agent-design.md)
> 状态：已获确认，按阶段执行

## 目标

在不改变已核验法条和召回数据的前提下，完成一个默认离线、可审计、固定顺序的咨询流水线：

`POST /api/consult` -> 会话恢复 -> 场景分类/事实抽取 -> 追问或本地检索 -> 确定性规则 -> 辖区/时效 -> 模板渲染 -> 审计与用量记录。

默认路径使用 `FakeProvider`，不访问网络；只有显式设置 `LLM_PROVIDER=deepseek` 且提供项目根目录 `.env` 中的 Key 时才访问 DeepSeek。模型永远不能生成或覆盖法条、引用、日期、辖区规则或 `verdict`。

## 当前基线

- 已核验法条：61 条，`placeholder_data=false`。
- 已核验召回样本：64 条，覆盖 `data/retrieval_benchmark.yaml` 中的 9 类场景。
- `Recall@5=1.000`，`scripts/verify_refs.py` 已通过。
- 现有配置、健康检查、本地 SQLite/FTS5 入库和检索测试保留。
- 工作区已有的 `README.md`、`app/playbooks/test_scenario.yaml`、`data/retrieval_benchmark.yaml`、`data/seed_statutes.yaml` 和 `docs/legal-data-verification-checklist.md` 改动不回退。
- 不修改根目录 `PLAN.md` 和 `新建 文本文档.txt`。

## 执行顺序

### 1. 配置与 Provider 契约

**文件**

- 修改 `app/config.py`、`.env.example`、`requirements.txt`。
- 新增 `app/providers/__init__.py`、`app/providers/base.py`、`app/providers/fake.py`、`app/providers/deepseek.py`、`app/providers/factory.py`。
- 新增 `app/agent/models.py`、`app/agent/errors.py` 中 Provider 需要的领域模型和异常。

**实现**

- 将默认 Provider 设为 `fake`，增加 `llm_provider`、DeepSeek base URL、模型、超时、重试、消息/槽位长度和价格配置；所有相对路径解析到项目根目录。
- 用 Pydantic 模型约束 `ExtractionResult`、槽位候选和 `UsageInfo`。未知字段、模型返回的 `verdict`、法条字段、Prompt 或 Key 字段一律拒绝。
- `FakeProvider` 返回可重复的分类和槽位结果，并支持测试注入的响应/异常。
- `DeepSeekProvider` 只用 `httpx.AsyncClient` 调用兼容 Chat Completions 的接口；Bearer Key 只存在内存请求头。将超时、网络错误、429、5xx、非法 JSON 归一为脱敏 `ProviderError`，最多重试一次。
- Provider 工厂明确区分 `fake`、`deepseek` 和缺 Key 配置错误；选择 DeepSeek 但缺 Key 时不得回退成 Fake。
- 清理与本阶段不再使用的 Anthropic/simpleeval 依赖，保留 `httpx`、`jinja2`、`pyyaml` 等已有运行依赖。

**验证**

- 测试工厂选择、缺 Key、Fake 确定性输出。
- 用 `httpx.MockTransport` 覆盖请求体、认证、超时、429/5xx 和非法 JSON。
- 检查仓库搜索结果中不出现真实 Key、Authorization 头或原始 Prompt 持久化逻辑。

### 2. SQLite 会话、Turn 和审计

**文件**

- 新增 `app/db/__init__.py`、`app/db/session.py`、`app/db/models.py`。
- 新增 `tests/test_db.py`。

**实现**

- 使用项目内 SQLite 文件和参数化 SQL，建立 `sessions`、`turns`、`audit_records`、`usage_daily` 表。
- 会话保存 UUID、场景、合并后的结构化槽位、追问轮次、状态和过期时间；Turn 保存用户消息、事实快照、规则命中、响应 JSON、Provider 元数据和创建时间。
- 审计记录保存阶段名称、状态、时间、playbook 版本、引用和错误类别，不保存 Key、Authorization Header 或完整请求。
- 所有时间统一使用 UTC ISO 格式；读取时清理过期会话及关联敏感内容。数据库路径由 Settings 解析，测试可以传临时路径。
- 数据库初始化幂等，SQLite 连接启用外键并设置合理 busy timeout。

**验证**

- 新建/恢复会话、过期会话拒绝、TTL 清理、Turn/审计写入和敏感字段不落库测试。

### 3. Playbook Schema、注册表与安全规则求值

**文件**

- 新增 `app/playbooks/schema.py`、`app/playbooks/registry.py`、`app/playbooks/evaluator.py`。
- 新增正式 `app/playbooks/deposit_deduction.yaml`。
- 保留并明确标注 `test_scenario.yaml` 为 `development_only`。
- 新增 `tests/test_playbooks.py`、`tests/test_rules.py`。

**实现**

- 用 Pydantic 严格校验 playbook 版本、ID、别名、槽位类型/范围/枚举、默认值、法条引用、规则分支、证据清单和升级条件。
- 注册表只从固定的 `app/playbooks` 目录加载，并拒绝重复 ID、未知 verdict、缺少默认分支、非法模板路径和未声明槽位。
- 规则采用结构化节点：`exists`、`eq`、`neq`、`in`、`gte`、`lte`、`contains`、`all`、`any`、`not`；不使用 `eval`、`exec` 或任意表达式解释器。
- 求值前补齐 optional 默认值，并返回可审计的 `RuleMatch`。默认分支只允许保守或升级 verdict。
- 押金 playbook 只使用已核验且有明确业务依据的法条引用，规则输出采用注册的保守 code，不由 YAML 任意发明结论。

**验证**

- 覆盖每个操作符、组合节点、类型错误、未知槽位、默认分支、注入字符串和未知 verdict。
- 逐一检查正式 playbook 引用数据库，运行 `scripts/verify_refs.py`。

### 4. 辖区与时效确定性模块

**文件**

- 新增 `app/jurisdiction/__init__.py`、`app/jurisdiction/schema.py`、`app/jurisdiction/rules.py`、`app/jurisdiction/data.yaml`。
- 新增 `tests/test_jurisdiction.py`。

**实现**

- 只登记本阶段明确支持的通用规则和明确辖区配置；未知辖区返回 `escalate`，不由模型推测地方标准。
- 从 playbook 声明的日期字段计算时效，处理跨闰年、当天到期和已过期；结果包含起算日、截止日、剩余天数和提示。
- 小额诉讼上限等需要地方数据的字段没有配置时保持未知，并在限制/升级信息中明确说明。

**验证**

- 覆盖无辖区、已登记辖区、未知辖区、闰年、到期当天和过期日期。

### 5. 结构化方案与 Jinja2 渲染

**文件**

- 新增 `app/rendering/__init__.py`、`app/rendering/models.py`、`app/rendering/renderer.py`。
- 新增固定模板 `app/rendering/templates/consultation_plan.j2`、`app/rendering/templates/evidence_request.j2`。
- 新增 `tests/test_rendering.py`。

**实现**

- 用 Pydantic 定义引用、证据、行动步骤、限制、时效、Verdict、审计草稿和 API 方案模型。
- Jinja2 环境只从项目内固定模板目录加载；模板名由已加载 playbook 映射，用户不能直接指定路径。
- 输出第一部分固定为 `evidence_now`，随后是时效、保守判断、法条原文引用、行动步骤和免责声明。
- 模型润色只接收锁定的非法律话术草稿；失败时保留原模板文本。模板不自行推导 verdict 或法条。

**验证**

- 校验引用只能由 `StatuteRecord` 转换、模板顺序、HTML/Jinja 注入转义、非法模板名和润色降级。

### 6. 固定 Pipeline 与 API

**文件**

- 新增 `app/agent/pipeline.py`。
- 新增 `app/api/schemas.py`、`app/api/consult.py`。
- 修改 `app/main.py`、`app/deps.py`、`app/api/health.py`（只扩展依赖检查）。
- 新增 `tests/test_pipeline.py`、`tests/test_api.py`。

**实现**

- 按设计的 13 个阶段固定编排：请求校验、会话、playbook、抽取、槽位合并/验证、最多两轮追问、强制+补充检索、规则、辖区/时效、锁定草稿、渲染、持久化、统一响应。
- 场景分类只接受注册表中的 ID；低置信度或未知场景返回安全的 `need_more_facts`/`escalate`。
- 每个缺失槽位使用 playbook 预声明 `ask` 文案；每个会话最多两轮，达到上限后 `can_ask_more=false`，不生成确定性法律结论。
- 强制引用先精确命中，再以用户消息和结构化事实做 FTS5 补充；数据库门禁、引用缺失和规则完整性失败均 fail closed。
- `verdict` 只取规则求值结果；API 响应统一包含 session/turn/audit ID、状态、追问、计划、引用和脱敏模型用量（不含 Key）。
- 将领域异常映射为 422/429/503/500，不返回堆栈、Prompt、Header 或秘密。
- 使用 FastAPI 依赖注入允许测试替换 Settings、Provider、数据库和限流器。

**验证**

- 首次咨询、追问、恢复会话、两轮上限、未知 session、过长输入、未知场景、规则/数据库故障和 Provider 故障集成测试。
- 默认 `TestClient` 流程不发网络请求。

### 7. 限流、熔断与费用记录

**文件**

- 新增 `app/limits/rate_limit.py`、`app/limits/circuit.py`、`app/limits/usage.py`。
- 修改 `app/config.py`、`app/deps.py`、`app/api/consult.py`。
- 新增 `tests/test_limits.py`。

**实现**

- 对服务端计费调用按客户端 IP 做日配额；用户自带 Key 不计入服务端配额。
- 使用 UTC 日界线累计 DeepSeek input/output token 和显式配置的价格；未配置价格时只记 token，不伪造金额。
- `is_tripped()` 的语义固定为“跳闸即拒绝”，超过日支出上限后服务端请求 503；每 UTC 日自动重置。
- 在真实 Provider 调用前检查限流/熔断，调用后记录 usage；错误消息只包含脱敏类别。

**验证**

- 三种 Key 模式、用户 Key 绕过服务端限制、日配额边界、熔断跳闸/次日恢复、费用累计和并发锁测试。

### 8. 迁移其余已核验场景

**文件**

- 新增正式 playbook：`prepaid_card.yaml`、`overtime_pay.yaml`、`return_refused.yaml`、`counterfeit_goods.yaml`、`training_refund.yaml`、`auto_renewal.yaml`、`renovation_default.yaml`、`small_claim_procedure.yaml`。
- 新增或修改 `app/jurisdiction/data.yaml`、模板和 registry 映射。
- 每个场景新增对应规则测试；不改已核验法条正文和召回样本。

**实现**

- 每个场景只引用 `data/seed_statutes.yaml` 中已命中的条文，声明完整槽位、默认值、证据清单、保守默认分支和升级条件。
- 对劳动场景使用劳动仲裁一年时效和独立行动提示；对小额诉讼场景保留程序性信息，不把程序条件扩写成裁判结论。
- 对未有足够核验资料支持的复杂事实，使用 `need_more_facts` 或 `escalate`，不凭模型知识补规则。

**验证**

- 64 条召回回归、全部正式 playbook 引用校验、每个规则分支至少一个单测和所有场景 API 烟测。

### 9. 文档、依赖清理与最终门禁

**文件**

- 修改 `README.md`、`.env.example`、`requirements.txt`，必要时补充 `docs/deepseek-local-test.md`。
- 不改 `PLAN.md` 和 `新建 文本文档.txt`。

**实现**

- 写清 DeepSeek 本地联调步骤、Key 只放 `.env`、默认 Fake 不联网、日志/数据库不存 Key。
- 添加离线入库、引用校验、召回、pytest 和启动命令。
- 处理 Windows 下 pytest 临时目录权限：使用工作区内可写的临时目录策略，确保重复运行不会因旧临时文件清理失败而误报。

**最终验收**

1. `scripts/verify_refs.py` 通过。
2. `scripts/check_recall.py` 输出 `Recall@5=1.000` 并通过门禁。
3. 默认离线 pytest 全绿。
4. `POST /api/consult` 至少完成押金场景的追问和最终方案闭环。
5. 关键响应、数据库和审计记录中不存在 API Key、Authorization Header、Prompt 或堆栈。
6. 显式 DeepSeek 联调只在用户填入 `.env` 后执行，输出脱敏状态和用量。

## 实施节奏

每完成一个阶段立即运行该阶段单测和数据门禁；发生失败时先修复再进入下一阶段。实现期间只在当前项目目录内读写，绝不联网抓取法条，也不修改用户已确认的数据文件。
