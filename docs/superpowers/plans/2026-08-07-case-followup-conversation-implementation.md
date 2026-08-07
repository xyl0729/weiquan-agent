# 案件续问与方案更新实施计划

> 日期：2026-08-07
> 依据：[案件续问与方案更新设计](../specs/2026-08-07-case-followup-conversation-design.md)
> 状态：设计已确认，按本计划实施

## 目标

把已有方案后的消息从“重新抽取并重复生成整份方案”改为受约束的案件续问：

- 普通追问返回针对性短答，不复制方案。
- 新事实或撤回事实由本地规则重算，并明确标记方案更新。
- 另一项纠纷只提示分案，不污染当前案件。
- 每个用户轮次只调用一个 Provider 方法。
- 公开法条严格限制为当前 Playbook 的 `legal_basis`。
- 新旧历史记录都能恢复，右侧摘要始终保留最近一份有效方案。

不修改 SQLite schema，不读取或输出 `.env`，自动化测试只使用 Fake Provider；全部自动化通过后仅执行一组两轮 DeepSeek 实测。

## 1. 续问领域模型与 Provider 协议

**文件**

- 修改 `app/agent/models.py`
- 修改 `app/providers/base.py`
- 修改 `tests/test_providers.py`

**先写测试**

- 为 `CaseContinuationContext`、`CaseContinuationResult` 和最近轮次模型增加严格 Pydantic 校验。
- 覆盖 `same_case`、`new_case`、事实补充、事实撤回、动作/法条引用去重及数量上限。
- 拒绝未知字段、越界置信度、同一槽位同时出现在 `facts` 与 `cleared_slots`、无效字段组合。
- 确认 `LLMProvider` 同时声明 `extract_facts(...)` 与 `continue_case(...)`。

**实施**

- 在 Agent 领域模型中加入受限续问上下文、锁定方案摘要、场景目录、最近轮次和 Provider 续问结果。
- 将续问结果的身份、请求 ID 和 usage 与事实抽取结果保持同一契约。
- 在 Provider Protocol 中新增 `continue_case(message, context)`，不改变现有事实抽取接口。

**验证**

```powershell
pytest -q tests/test_providers.py --basetemp=.test-tmp/providers-contract
```

## 2. Fake 与 DeepSeek 续问实现

**文件**

- 修改 `app/providers/fake.py`
- 修改 `app/providers/deepseek.py`
- 修改 `tests/test_providers.py`

**先写测试**

- Fake Provider 可注入抽取结果、续问结果和错误，并分别统计两类方法调用次数。
- Fake Provider 默认能确定性返回普通续问、事实更新和已注册新场景。
- DeepSeek 请求使用 JSON mode、`temperature: 0` 和受限上下文。
- DeepSeek 输出严格拒绝未知槽位、重复或越界 action/citation refs、URL、法条结论字段、额外字段及非法路由。
- 验证 Provider identity、request ID、usage 和底层重试行为。

**实施**

- 扩展 Fake Provider 构造参数，为测试提供独立的续问结果队列。
- 构建 DeepSeek 续问系统提示，只允许识别路由、提取本轮明确事实及生成受约束短答。
- 复用现有 HTTP 请求、重试、usage 和敏感字段拦截逻辑。
- 在 Provider 边界校验槽位白名单、场景白名单、动作白名单和法条白名单。

**验证**

```powershell
pytest -q tests/test_providers.py --basetemp=.test-tmp/providers
```

## 3. 案件上下文与精确法条加载

**文件**

- 新增 `app/agent/continuation.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/rendering/models.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_rendering.py`

**先写测试**

- 上下文最多携带最近 4 个已完成轮次，单条用户消息最多 500 字、短答最多 800 字、总量最多 4000 字。
- 上下文只含当前 Playbook、已验证 facts、最近有效方案、正式场景目录和公开短历史。
- 所有正式 Playbook 的完整方案 citations 与自身 `legal_basis` 顺序完全一致。
- 网购退货查询不会混入租赁、劳动或其他场景法条。
- 任一强制引用缺失、元数据未验证或 schema version 不匹配时关闭失败。

**实施**

- 用窄职责 `CaseContextBuilder` 从当前 session、历史 turns 和最近有效 plan 构建续问上下文。
- 为动作生成稳定临时引用 `A1`、`A2`，并携带相应 Playbook 原文。
- 将咨询方案法条读取改为按 `legal_basis` 逐条调用 `get_statute_by_ref(...)`，停止把 message/facts 送入 FTS 扩充公开引用。
- 收紧渲染模型，确保 draft 不会追加 Playbook 白名单外的法条。

**验证**

```powershell
pytest -q tests/test_pipeline.py tests/test_rendering.py --basetemp=.test-tmp/context
```

## 4. 流水线双路径与单轮单调用

**文件**

- 修改 `app/agent/pipeline.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_limits.py`

**先写测试**

- 没有历史方案时只调用 `extract_facts(...)`。
- 已有历史方案时只调用 `continue_case(...)`。
- 无事实变化返回 `followup_answer`，`plan`/`verdict` 为空。
- 新增、更正或撤回 facts 后重新执行本地规则，返回 `plan_update`。
- 另一正式场景和高置信未知新纠纷返回 `new_case`，当前 scenario/facts/status 不变。
- Provider 或重算失败时不写成功 turn，不更新 session facts/status，并保留最近方案。
- 每轮 `before_call`/`after_call` 与 usage 只记一次。

**实施**

- `PipelineResult` 增加 `turn_kind` 和可选 `reply`。
- 通过“历史中是否存在有效 plan”选择事实收集或案件续问路径。
- 对续问结果执行固定优先级：分案、事实变更重算、无变化短答。
- 短答动作和 citations 只从后端白名单映射，不直接采纳模型生成的动作或法条正文。
- 保持首次方案及追问上限原有行为；`plan_update` 复用同一个本地方案构建函数。
- 审计中记录实际 Provider 路径、最终 `turn_kind` 和真实执行阶段。

**验证**

```powershell
pytest -q tests/test_pipeline.py tests/test_limits.py --basetemp=.test-tmp/pipeline
```

## 5. API 契约与旧历史兼容投影

**文件**

- 修改 `app/api/schemas.py`
- 修改 `app/history/service.py`
- 修改 `tests/test_api.py`
- 修改 `tests/test_history.py`

**先写测试**

- 校验五种 `turn_kind` 与 `plan`、`verdict`、`questions`、`reply` 的合法组合。
- 新响应保存并恢复后完全一致。
- 无新字段的旧事实收集、首份方案和更新方案可恢复。
- 连续相同旧 plan 投影为固定的紧凑 `followup_answer`。
- 旧响应法条按当前 Playbook 白名单过滤；过滤发生时不继续公开旧 `rendered_text`。
- 投影只在内存中完成，不改写 SQLite 原记录。

**实施**

- 新增 `TurnKindResponse`、`ReplyResponse` 和 `NewCaseResponse` 等公开模型。
- 新写响应必须显式带 `turn_kind`；历史读取允许旧响应先通过兼容模型再投影为新契约。
- 历史服务顺序扫描 turns，以规范化 verdict/plan 判断首份、更新或重复方案。
- 根据 session 场景加载当前 Playbook 白名单，过滤 citations 与 `reply.citation_refs`。
- 对旧重复方案生成固定说明：“本轮未记录到新的方案变化，前一份方案仍然有效。”

**验证**

```powershell
pytest -q tests/test_api.py tests/test_history.py --basetemp=.test-tmp/history-api
```

## 6. 前端续问、分案和稳定摘要

**文件**

- 修改 `app/web/js/api.js`
- 修改 `app/web/js/render.js`
- 修改 `app/web/js/app.js`
- 按需小幅修改 `app/web/styles.css`
- 修改 `tests/test_api.py`

**先写检查**

- 静态脚本仍不使用 `innerHTML`、`insertAdjacentHTML` 或 `localStorage`。
- API 校验器拒绝非法 `turn_kind`/`reply` 组合。
- 浏览器流程覆盖初始方案、普通短答、方案更新、分案提示和历史刷新。

**实施**

- 对话区按 `turn_kind` 显示不同标题和内容；短答只显示正文、最多 3 项动作及本轮法条。
- `plan_update` 明确显示“方案已根据新信息更新”。
- `new_case` 显示“作为新咨询继续”，点击后重置当前前端会话并把原消息预填到新咨询输入框，不自动发送。
- 右侧摘要分别取最新响应的状态/待补充项，以及向后扫描得到的最近有效 plan/verdict/citations。
- 延续现有白色工作台，不做无关视觉重构。

**验证**

```powershell
pytest -q tests/test_api.py --basetemp=.test-tmp/web-contract
```

## 7. 全量自动化与回归修复

**文件**

- 按失败用例最小范围修正相关模块
- 不修改 `.env` 或 `.env.example`

**实施与验证**

1. 覆盖 9 个正式 Playbook 的首次方案、普通续问和 citation 精确集合。
2. 补齐相似追问、事实补充/更正/撤回、跨场景、错误回滚和调用计数测试。
3. 使用新的项目内 basetemp，避开已有无权限临时目录。
4. 执行法条引用与召回检查。

```powershell
pytest -q --basetemp=.test-tmp/full
python scripts/verify_refs.py
python scripts/check_recall.py
```

## 8. 浏览器验收与最小 DeepSeek 实测

**浏览器验收**

- 仅监听 `127.0.0.1` 启动本地服务。
- 使用 Fake Provider 检查桌面与 360px 手机视口的四种连续状态。
- 确认短答后右栏保留方案，更新后切换到新方案，分案按钮只预填不发送。
- 检查长中文、3 个动作和 3 个 citation 不溢出、不重叠、无横向滚动。

**DeepSeek 实测**

- 不读取、显示或记录 `.env`，由应用自行加载已保存配置。
- 只执行一组两轮网购退货会话：
  1. `网购商品与描述不符，商家拒绝退款`
  2. `他还是不配合怎么办`
- 验证第二轮为 `followup_answer` 且 `plan == null`，两轮均有独立 request ID 和非零 usage。
- 检查第二轮引用是退货 Playbook 白名单子集。
- 检查 API 响应、应用日志和 SQLite 公共字段不包含 Key、Authorization Header、完整 Prompt 或 Provider 原始 JSON。

## 完成标准

- 原复现会话的第二、第三轮不再重复整份方案。
- 普通续问、事实变更和新纠纷分别稳定进入正确分支。
- 每个用户轮次只调用一个 Provider 方法并只累计一次 usage。
- 所有公开 citation 都属于当前 Playbook。
- 历史兼容投影、右侧方案保持和分案预填均可用。
- 全量自动化、桌面/移动浏览器验收和最小 DeepSeek 两轮实测全部通过。
