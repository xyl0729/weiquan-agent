# 智能路由、覆盖分流与 Provider 切换实施计划

> 日期：2026-08-08
> 依据：[智能路由、场景扩展与模型切换设计](../specs/2026-08-07-smart-routing-coverage-and-provider-switch-design.md)
> 状态：设计与安全边界已确认，按本计划实施

## 目标与边界

在保留现有 9 个正式 Playbook、确定性规则、法条引用、附件隔离、续问和历史兼容能力的前提下：

- 用户直接描述问题，系统自动路由，不再回退到旧分类菜单。
- 15 个新增 Topic 只进入 `unverified_guidance`；紧急信号优先进入 `emergency_guidance`。
- 未核验和紧急结果不得包含正式 `plan`、`verdict`、Playbook 法条或时效结论。
- 正式方案和安全指导都提供结构化、可发送的沟通指南。
- 每次请求可在 `fake` 与 `deepseek` 间选择，并公开实际 Provider、模型、请求 ID 和 usage。
- DeepSeek 不可用或失败时不静默降级；Key 不进入浏览器、响应、日志或 SQLite。

本阶段不新增正式 Playbook 或法条数据，不读取或输出 `.env`、`.env.example`，不访问或清理 `.test-tmp`。测试统一使用 `D:\agent development\pytest-temp-*` 作为独立 `--basetemp`。

## 1. 覆盖领域模型与 Topic 注册表

**文件**

- 修改 `app/agent/models.py`
- 新增 `app/agent/routing.py`
- 新增 `tests/test_routing.py`
- 修改 `tests/test_pipeline.py`

**先写测试**

- 校验 `CoverageMode`、`RiskFlag`、`CoverageResult`、`GuidanceResult` 和新 turn 类型的严格 Schema。
- 现有 9 个 Topic 映射到同名正式 Playbook。
- 15 个新增 Topic 均固定为 `unverified_guidance` 且 `playbook_id=null`。
- 未知 Topic 使用安全长尾结果，不回退旧分类问题。
- Provider 夹带 `coverage_mode`、`scenario_id`、引用或未知字段时不能直接生效。

**实施**

- 建立只读 `TopicRegistry`，记录 Topic ID、中文标签、别名、基线覆盖状态和可选 Playbook ID。
- 分离 Provider 候选抽取与服务端 `RouteResult`；覆盖模式和正式场景只由后端派生。
- 为未核验事实使用窄化的通用事实 Schema，避免写入正式 Playbook 槽位。
- 保持现有正式场景 ID、事实模型和公开响应兼容。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_routing.py tests/test_pipeline.py --basetemp='D:\agent development\pytest-temp-routing'
```

## 2. 安全信号门与未核验指导

**文件**

- 修改 `app/agent/routing.py`
- 新增 `app/agent/guidance.py`
- 修改 `app/providers/fake.py`
- 修改 `app/providers/deepseek.py`
- 修改 `tests/test_routing.py`
- 新增 `tests/test_guidance.py`
- 修改 `tests/test_providers.py`

**先写测试**

- 正在发生的人身危险、未成年人伤害、紧急就医、疑似刑事侵害、诈骗止损和证据即将灭失优先进入 `emergency_guidance`。
- 普通教育、医疗、交通、劳动解除、物业等输入进入对应未核验 Topic。
- 紧急指导先返回停止危险、就医、报警或求助顺序，并明确不要为取证继续置身危险。
- 缺少本地已核验号码时不生成具体电话号码。
- 未核验指导只包含取证、普通沟通、机构类型、限制和最多一个关键问题。

**实施**

- 在普通主题路由前运行确定性的 `SafetySignalGate`。
- 扩展 Fake Provider，使 15 个 Topic 的测试样本完全离线且结果确定。
- 收紧 DeepSeek 候选抽取 JSON 契约，只允许 Topic 候选、通用事实、风险信号和置信度。
- `GuidanceBuilder` 使用后端模板生成中性指导，不采纳模型法条或责任结论。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_routing.py tests/test_guidance.py tests/test_providers.py --basetemp='D:\agent development\pytest-temp-guidance'
```

## 3. Provider 目录、解析器与按请求选择

**文件**

- 新增 `app/providers/catalog.py`
- 修改 `app/providers/factory.py`
- 修改 `app/config.py`
- 修改 `app/deps.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/api/schemas.py`
- 修改 `app/api/consult.py`
- 新增 `app/api/providers.py`
- 修改 `app/main.py`
- 修改 `tests/test_config.py`
- 修改 `tests/test_deps.py`
- 修改 `tests/test_api.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_providers.py`

**先写测试**

- Provider 目录只公布 `fake` 和 `deepseek`，不包含 Secret 或敏感异常。
- `ConsultRequest.provider_id` 可选；省略时使用服务端默认值。
- 未知 Provider 返回稳定的 `invalid_provider`，不可用 Provider 返回 `provider_unavailable`。
- Pipeline 按每次请求解析 Provider，同一会话下一轮可以切换。
- DeepSeek 失败不触发 Fake 调用，不覆盖已有会话事实。
- Fake 路径不创建网络客户端。

**实施**

- `ProviderCatalog` 只负责公开元数据；`ProviderResolver` 只负责白名单解析和可用性校验。
- Pipeline 不再持有固定 Provider，改为在每轮开始时解析请求 Provider。
- 增加 `GET /api/providers`，返回显示名、模型、可用状态、离线标志、默认标志和非敏感原因。
- 保留现有 DeepSeek 重试、usage、request ID 和敏感字段拒绝机制。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py tests/test_deps.py tests/test_providers.py tests/test_pipeline.py tests/test_api.py --basetemp='D:\agent development\pytest-temp-provider'
```

## 4. Pipeline 三路径与失败原子性

**文件**

- 修改 `app/agent/pipeline.py`
- 修改 `app/agent/continuation.py`
- 修改 `app/agent/errors.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_limits.py`

**先写测试**

- 正式 Topic 继续执行现有事实追问、法条加载和确定性规则路径。
- 新增识别域直接返回 `unverified_guidance`，不读取无关 Playbook 或法条。
- 紧急信号跳过普通分类等待并返回 `emergency_guidance`。
- 最多只追问一个会改变下一步的事实；现有正式路径的两轮上限保持不变。
- Provider、路由、指导生成或持久化失败时不写成功 turn，不污染 session。
- Provider 切换后已确认 facts、正式场景和规则结果保持一致。

**实施**

- 将 Pipeline 编排拆成正式方案、未核验指导和紧急指导三条显式路径。
- 统一返回 `turn_kind`、`coverage` 和实际 Provider 元数据。
- 对未核验与紧急结果实施关闭失败：没有正式 `plan`、`verdict` 或 citations。
- 保留案件续问逻辑；未核验 turn 的后续消息重新经过安全门和 Topic 路由。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_pipeline.py tests/test_limits.py --basetemp='D:\agent development\pytest-temp-pipeline'
```

## 5. 结构化沟通指南与历史兼容

**文件**

- 修改 `app/rendering/models.py`
- 修改 `app/rendering/renderer.py`
- 修改 `app/rendering/templates/consultation_plan.j2`
- 修改 `app/api/schemas.py`
- 修改 `app/history/service.py`
- 修改 `tests/test_rendering.py`
- 修改 `tests/test_history.py`
- 修改 `tests/test_api.py`

**先写测试**

- 沟通指南包含收件对象、渠道、发送时机、目标、完整正文、发送后动作、升级动作和发送前必需补充项。
- 正式正文只能引用已确认事实和当前 Playbook 声明槽位。
- 未核验正文只能使用中性表达，不断言违法、责任或必然赔偿。
- `required_before_send` 非空时不会生成看似完整但含虚构值的正文。
- 新 turn 保存并恢复后结构一致；无新字段的旧 turn 仍可读取。
- 历史投影不会把未核验结果伪装成正式方案。

**实施**

- 增加共享 `CommunicationGuide` 模型，并分别由正式渲染器和 `GuidanceBuilder` 构造。
- 新公开响应增加可选 `coverage`、`guidance` 和结构化沟通指南。
- 历史服务继续做内存兼容投影，不重写旧 SQLite 记录。
- 每个 turn 保存实际 Provider、模型、请求 ID、usage 和非敏感错误类别。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_rendering.py tests/test_history.py tests/test_api.py --basetemp='D:\agent development\pytest-temp-response'
```

## 6. 前端 Provider 切换与指导展示

**文件**

- 修改 `app/web/index.html`
- 修改 `app/web/styles.css`
- 修改 `app/web/js/api.js`
- 修改 `app/web/js/state.js`
- 修改 `app/web/js/app.js`
- 修改 `app/web/js/render.js`
- 修改 `tests/test_api.py`

**先写检查**

- 页面启动时读取 `/api/providers`，默认选中服务端默认 Provider。
- 不可用 Provider 可见但不可选，并显示非敏感原因。
- 每次咨询提交当前 `provider_id`；失败后不自动改选 Fake。
- 响应和历史显示实际 Provider、模型、请求 ID 与 usage。
- `formal`、`unverified_guidance`、`emergency_guidance` 有清晰且不混淆的标题和内容层级。
- 动态渲染继续禁止 `innerHTML`、`insertAdjacentHTML` 和 Secret 持久化。

**实施**

- 在现有工作台工具区加入紧凑 Provider 选择控件，不重做整体视觉。
- 对覆盖边界、紧急动作、取证清单和结构化话术增加可扫描区域。
- 正式方案继续使用现有摘要；未核验和紧急结果不渲染空的正式结论区域。
- 保持桌面三栏和移动单列布局稳定，长中文和 usage 文本不溢出。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_api.py --basetemp='D:\agent development\pytest-temp-web'
```

## 7. 全量离线回归

**实施与验证**

1. 为现有 9 个正式 Playbook、15 个未核验 Topic、未知长尾和紧急信号补齐路由样本。
2. 验证 Provider 切换、错误原子性、历史兼容和沟通指南事实白名单。
3. 使用全新的 workspace 外侧 basetemp，避开现有无权限临时目录。
4. 运行完整测试、法条引用校验和召回门禁。

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp='D:\agent development\pytest-temp-full'
.\.venv\Scripts\python.exe scripts/verify_refs.py
.\.venv\Scripts\python.exe scripts/check_recall.py
```

## 8. 浏览器验收与可选 DeepSeek 联调

**浏览器验收**

- 仅监听 `127.0.0.1` 启动本地服务。
- 用 Fake Provider 验证现有正式场景、新增未核验 Topic、紧急信号、未知长尾、续问和历史恢复。
- 检查 1366x768、936x900、390x844 和 360x800，无重叠、裁切、横向滚动或不可达控件。
- 验证 Provider 状态、实际模型元数据、覆盖边界和完整沟通正文均可见。

**DeepSeek 联调**

- 不读取、显示或记录 `.env`；由应用按现有配置自行判断可用性。
- 仅当 `/api/providers` 明确显示 DeepSeek 可用，且用户明确选择 DeepSeek 时执行一个最小请求。
- 验证返回的 Provider、模型、请求 ID 和 usage 与实际调用一致。
- 若不可用则明确记录“未执行真实联调”，不以 Fake 结果代替。

## 完成标准

- 旧分类菜单不再是未命中的回退路径。
- 9 个正式 Playbook 的法条和规则行为保持不变。
- 15 个新增 Topic 只输出未核验或紧急指导，`playbook_id=null`。
- 紧急风险优先返回安全动作。
- 正式及未核验话术均结构化、可执行且不含虚构事实。
- Provider 可按请求切换，失败不静默降级，真实元数据可追溯。
- 新旧历史均可读取，Key 不进入前端、响应、日志或 SQLite。
- 完整离线测试、引用校验、召回门禁和桌面/移动浏览器验收全部通过。
