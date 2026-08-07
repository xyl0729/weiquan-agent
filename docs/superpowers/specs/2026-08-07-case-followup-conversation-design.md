# 案件续问与方案更新设计

## 文档状态

- 日期：2026-08-07
- 版本：v1
- 状态：设计已获用户确认并完成自检，待用户复核
- 项目目录：`D:\agent development\weiquan-agent`

本文档定义维权 Agent 在形成方案后的多轮对话行为，解决“用户无论追问什么，系统都再次返回几乎相同的完整方案”以及“全文检索把无关法条带入方案”的问题。

## 1. 问题与根因

当前流水线对每一轮消息都执行同一条路径：

1. DeepSeek 只抽取场景和事实。
2. 后端把新事实与会话事实合并。
3. 本地规则重新求值。
4. 法条检索、方案构建和模板渲染重新执行。

对于“那不配合怎么办”“他不配合怎么办”这类续问，模型没有抽取到新的槽位，已有事实保持不变。后端因此使用相同事实再次执行相同规则和固定模板，产生另一份相同的完整方案。数据库中的每一轮都有独立的 DeepSeek 请求记录，问题不在前端缓存。

现有法条路径还会在强制召回 Playbook 指定法条后，用用户本轮文本继续做全文检索。泛化续问可能命中与当前纠纷无关的法条，例如在网购退货案件中带入住房租赁条款。

前端右侧案件摘要只读取最后一轮响应。续问改为短回复后，如果不调整摘要选择逻辑，原有完整方案会从右栏消失。

## 2. 目标

本次改造必须同时满足：

1. 首次形成方案时仍由本地 Playbook、规则、管辖和模板生成完整方案。
2. 已有方案后的普通追问返回简短、直接、针对当前问题的回答，不重复整份方案。
3. 用户补充或纠正会影响规则的事实时，重新计算并明确展示“方案已更新”。
4. 用户切换到另一项纠纷时，不把新纠纷事实写入当前案件，而是建议建立新咨询。
5. 每个用户轮次最多调用一个 Provider 方法，不执行“先抽取、再回答”的双模型调用。
6. 用户可见的法条只能来自当前 Playbook 明确声明的 `legal_basis`。
7. 短回复出现后，右侧仍保留最近一份完整方案。
8. 已保存的旧响应无需数据库迁移，恢复历史时仍可安全读取。
9. Provider 失败或输出不合规时不覆盖既有案件事实和方案。
10. 自动化测试覆盖全部正式场景，最后只做一组最小 DeepSeek 真实联调。

## 3. 非目标

本次不包括：

- 开放式联网法律搜索或让模型自行选择新法条。
- 让模型修改 Playbook verdict、规则结果、时效或管辖结论。
- 自动合并两个案件。
- 自动替用户发送投诉、通知或诉讼材料。
- 修改现有 SQLite 表结构。
- 为附件、OCR、账号或跨设备同步预留实现。

## 4. 核心不变量

系统实现必须保持以下不变量：

- **规则归后端所有**：模型只能识别路由、提取本轮明确事实并生成受约束的说明文字。
- **方案归 Playbook 所有**：verdict、evidence、actions、limitations、time limit 和 jurisdiction 继续由本地数据与规则生成。
- **引用使用白名单**：响应中的每个 citation ref 都必须属于当前 Playbook 的 `legal_basis`。
- **单轮单模型路径**：一个用户轮次在 `extract_facts(...)` 与 `continue_case(...)` 中二选一。
- **案件不交叉污染**：不同场景不会覆盖当前 session 的 `scenario_id` 或 facts。
- **失败不产生半成品**：模型失败、法条数据失败、规则失败或持久化失败时，不返回部分方案。
- **既有方案可恢复**：普通续问不需要在每轮响应和数据库中复制完整方案。

## 5. 轮次类型

公开响应新增 `turn_kind`，新产生的响应必须明确使用以下一种值：

| `turn_kind` | 含义 | `plan` | `reply` |
| --- | --- | --- | --- |
| `fact_collection` | 尚在识别场景或补齐必填事实 | `null` | `null` |
| `initial_plan` | 当前案件第一次形成方案 | 完整方案 | `null` |
| `plan_update` | 新事实使既有方案重新求值 | 更新后的完整方案 | `null` |
| `followup_answer` | 事实未变化，回答当前追问 | `null` | 针对性短回复 |
| `new_case` | 本轮主要描述另一项纠纷 | `null` | 分案提示 |

会话的业务 `status` 继续只使用 `need_more_facts`、`ready`、`escalate`。`turn_kind` 表示本轮如何展示，`status` 表示当前案件整体状态，两者不能互相替代。

只要历史中已经存在一份有效 `plan`，后续轮次就进入案件续问路径。不能只检查 `ready` 或 `escalate`，因为少数保守规则可能在 `need_more_facts` 状态下仍形成阶段性方案；按“是否已有方案”判断可以一并避免这类方案重复。

## 6. 架构边界

### 6.1 `ConsultationPipeline`

流水线负责选择两条互斥路径：

- **事实收集路径**：没有历史方案时调用 `extract_facts(...)`，沿用现有场景识别、必填槽位追问和首次方案生成。
- **案件续问路径**：已有历史方案时调用 `continue_case(...)`，再由后端决定 `plan_update`、`followup_answer` 或 `new_case`。

流水线仍是唯一可以更新 session、写入 turn 和记录审计事件的组件。

### 6.2 `CaseContextBuilder`

新增一个窄职责组件或等价私有模块，负责构建续问上下文：

- 当前 Playbook 和已验证 facts。
- 最近一份有效方案的 verdict、summary、actions、evidence 和 limitations。
- 当前 Playbook 允许的法条及其 purpose。
- 全部正式场景的 id、名称和 aliases，仅用于识别分案。
- 最近最多 4 个已完成轮次的简化上下文。

最近上下文从新到旧按总字符上限截断，单条用户文本最多 500 字，单条 Agent 短回复最多 800 字，总量最多 4000 字。不放入完整模板文本、请求头、Provider 原始响应或密钥。

### 6.3 `LLMProvider`

Provider 协议新增：

```python
async def continue_case(
    message: str,
    context: CaseContinuationContext,
) -> CaseContinuationResult:
    ...
```

DeepSeek 是唯一真实 Provider。Fake Provider 实现同一协议，只用于离线自动化测试。

### 6.4 法条读取

面向咨询方案的法条读取改为按 Playbook `legal_basis` 精确加载：

1. 检查法条库 `placeholder_data == false` 和 schema version。
2. 按 `legal_basis` 声明顺序逐条 `get_statute_by_ref(...)`。
3. 任一强制引用缺失即失败关闭。
4. 不把本轮 message 或 facts 送入 FTS 来扩充用户可见引用。

通用检索模块可以继续服务检索基准测试，但不能再向咨询响应追加引用。

## 7. Provider 续问契约

### 7.1 输入

Provider 只接收经过后端整理的受限对象：

```json
{
  "current_scenario": {
    "id": "return_refused",
    "name": "退货换货被拒",
    "slot_definitions": {}
  },
  "registered_scenarios": [
    {
      "id": "deposit_deduction",
      "name": "租房押金扣除",
      "aliases": ["房东扣押金"]
    }
  ],
  "existing_facts": {
    "issue_type": "quality_problem"
  },
  "locked_case": {
    "verdict_label": "质量不符合要求时可核对退换修等救济",
    "key_point": "商品不符合质量要求时，应根据约定、收货时间和解除条件核对退货、更换、修理及必要运输费用。",
    "summary": "依据当前已确认事实，规则结果为质量问题救济路径。",
    "actions": {
      "A1": "立即保存商品页面、订单、物流、退货规则和商家拒绝页面的完整截图。",
      "A2": "核对实际收货日期，不要用下单日或付款日替代七日期间的起点。"
    },
    "evidence": ["商品页面、订单详情、付款记录、物流签收和实际收货日期"],
    "limitations": ["商品是否存在质量瑕疵，需要结合具体商品和证据判断。"],
    "citations": {
      "消费者权益保护法.第二十四条": {
        "law_name": "中华人民共和国消费者权益保护法",
        "article_no": "第二十四条",
        "content": "该字段由后端从已核验法条库读取完整条文。",
        "purpose": "质量不符合要求时的退货、更换、修理和必要运输费用"
      }
    }
  },
  "recent_turns": [],
  "current_message": "他不配合怎么办"
}
```

动作使用后端生成的稳定临时引用 `A1`、`A2`，引用只在本次 Provider 请求内有效，不写入 Playbook。

### 7.2 输出

Provider 只能返回一个 JSON 对象：

```json
{
  "route": "same_case",
  "scenario_id": "return_refused",
  "facts": {},
  "cleared_slots": [],
  "answer": "先把商家的拒绝理由固定下来，再通过平台投诉入口提交完整证据；仍不处理时，保留平台结论后选择行政投诉或诉讼。",
  "action_refs": ["A1", "A5"],
  "citation_refs": ["消费者权益保护法.第二十四条"],
  "confidence": 0.98
}
```

字段约束：

- `route` 只能是 `same_case` 或 `new_case`。
- `scenario_id` 只能是当前场景、已注册正式场景或 `unsupported`。
- `facts` 只能包含当前 Playbook 已声明、且本轮消息明确表达的槽位。
- `cleared_slots` 只能包含用户在本轮明确撤回或表示无法确认的已有槽位。
- 同一个槽位不能同时出现在 `facts` 和 `cleared_slots`。
- `answer` 在 `same_case` 时必须为 1 至 800 字；`new_case` 时必须为 `null`。
- `action_refs` 最多 3 项，必须是输入动作白名单的子集且不得重复。
- `citation_refs` 最多 3 项，必须是输入引用白名单的子集且不得重复。
- `confidence` 必须在 0 到 1 之间。
- 输出禁止 verdict、法律结论字段、URL、来源链接、Prompt、请求头和密钥字段。

短回复正文不得直接写 URL 或法条编号。需要展示法律依据时，模型只返回 `citation_refs`，后端再附加已验证的完整 citation 对象。

### 7.3 后端最终路由

模型的 `route` 只是受约束的分类结果，后端按以下固定优先级决定本轮类型：

1. `scenario_id` 是另一正式场景，或高置信度 `new_case + unsupported`：返回 `new_case`，当前 facts 和 scenario 不变。
2. 否则验证 `facts` 与 `cleared_slots`，应用到当前 facts 后做规范化比较。
3. 规范化 facts 有变化：忽略模型的 answer 和 refs，重新执行本地规则并返回 `plan_update`。
4. facts 无变化：校验 answer、动作和引用白名单，返回 `followup_answer`。

新场景不会在当前 session 中自动创建。分案提示只让用户确认并开始新咨询，避免错误迁移上下文。
`new_case` 本轮仍作为一条成功 turn 保存在原案件中，但持久化时沿用原 scenario、facts 和 status。

## 8. 方案生成与引用安全

`initial_plan` 和 `plan_update` 使用同一套本地方案函数，避免两条规则实现漂移：

1. 验证并应用事实默认值。
2. 执行 Playbook 规则和 escalation。
3. 计算 jurisdiction 和 time limit。
4. 精确加载 Playbook 法条。
5. 构建 locked draft。
6. 渲染固定模板。

模型输出不能参与上述步骤。

所有公开 citations 必须满足：

```text
set(response.citations[*].ref)
  ⊆ set(current_playbook.legal_basis[*].ref)
```

完整方案默认包含 Playbook 声明的全部引用并保持声明顺序。短回复只返回模型选择且通过白名单验证的子集。网购退货案件不得出现住房租赁、劳动或其他场景的引用。

## 9. 公开 API 契约

`POST /api/consult` 保持路径和请求体不变。响应新增两个兼容字段：

```json
{
  "turn_kind": "followup_answer",
  "reply": {
    "text": "先固定商家的拒绝理由……",
    "suggested_actions": [
      "立即保存商品页面、订单、物流、退货规则和商家拒绝页面的完整截图。"
    ],
    "citation_refs": [
      "消费者权益保护法.第二十四条"
    ],
    "new_case": null
  }
}
```

分案响应示例：

```json
{
  "turn_kind": "new_case",
  "status": "ready",
  "plan": null,
  "reply": {
    "text": "这看起来是另一项纠纷，建议单独建立咨询，避免两件事相互混淆。",
    "suggested_actions": [],
    "citation_refs": [],
    "new_case": {
      "scenario_id": "deposit_deduction",
      "label": "租房押金扣除"
    }
  },
  "citations": []
}
```

约束关系：

- `fact_collection`：`questions` 或保守 limitations 至少有一项。
- `initial_plan`、`plan_update`：`plan` 和 `verdict` 非空，`reply` 为空。
- `followup_answer`：`reply` 非空，`plan` 和 `verdict` 为空。
- `new_case`：`reply.new_case` 非空；未知但明确独立的纠纷允许 `scenario_id` 为 `null`。
- `reply.citation_refs` 必须与顶层 `citations[*].ref` 完全一致。
- `suggested_actions` 由后端根据 `action_refs` 映射成 Playbook 原文，不能直接采用模型生成的动作文字。

为读取旧记录，Pydantic schema 暂时允许 `turn_kind` 和 `reply` 缺省；所有新写入响应必须显式填写 `turn_kind`。

## 10. 持久化与旧历史兼容

SQLite 已把完整公开响应保存在 `turns.response_json`，因此不新增列、不升级 schema version。

历史服务按时间顺序读取旧 turns，并在内存中生成兼容投影：

1. 无 plan 且有 questions：推断为 `fact_collection`。
2. 第一份 plan：推断为 `initial_plan`。
3. 与上一份有效 plan 内容不同：推断为 `plan_update`。
4. 与上一份有效 plan 内容相同：投影为紧凑的 `followup_answer`，使用固定说明“本轮未记录到新的方案变化，前一份方案仍然有效”，不再向浏览器发送重复 plan。

方案比较使用规范化 JSON，只比较结构化 verdict 和 plan，忽略 usage、request id、audit id、渲染文本和 citation 排序噪声。

读取历史时还要按该 session 的 Playbook 白名单过滤旧 citations。若旧 `rendered_text` 包含被过滤的额外引用，则该兼容投影把 `rendered_text` 设为 `null`，避免无关法条继续通过 API 暴露。数据库原记录不被改写，前一份有效结构化方案仍可阅读。

新响应不会复制前一份 plan。恢复会话时，前端和历史服务都通过向后扫描找到最近一份有效方案。

## 11. 前端行为

### 11.1 对话区

- `fact_collection`：继续显示待补充问题。
- `initial_plan`：显示“维权方案”和完整方案内容。
- `plan_update`：显示“方案已根据新信息更新”的明确标记，再展示更新后的完整方案。
- `followup_answer`：只显示回复正文、最多 3 个已批准行动和本轮选中的法条，不重复完整方案。
- `new_case`：显示分案说明和“作为新咨询继续”命令。

“作为新咨询继续”只重置当前前端会话并把该轮用户原文填入新咨询输入框，不自动发送，也不删除原案件。

### 11.2 右侧案件摘要

右侧不再只使用最后一轮响应，而是分别选择：

- 当前 status 和待补充问题：使用最新响应。
- verdict、summary、evidence 和完整法律依据：从后向前找到最近一份带 plan 的响应。

因此普通续问和分案提示不会擦除既有方案。`plan_update` 出现后，右栏自动切换到更新后的方案。

### 11.3 历史恢复

前端 API 校验器接受新增字段并校验字段组合。后端已把旧响应投影成明确的 `turn_kind`，刷新页面后不会重新展开连续重复方案，也不会把旧的无关 citation 放回右栏。

## 12. 错误处理

- Provider 超时、限流、网络失败或输出不合规：本轮请求失败，不写成功 turn，不修改 session facts/status；前端保留草稿和已有方案。
- 法条库元数据异常或 Playbook 引用缺失：在调用 Provider 前失败，避免无效模型消耗。
- `facts`、`cleared_slots`、action refs 或 citation refs 越界：作为 `provider_output_invalid` 失败，不做部分采纳。
- 方案重新求值失败：不返回模型 answer 作为降级结果。
- `new_case` 结果低于分类置信阈值：将本轮 Provider 输出判为无效，不迁移案件、不写成功 turn。模型不确定时必须返回当前场景的 `same_case` 短回复，不能让后端猜测。
- 持久化失败：沿用现有安全错误，不向前端暴露 SQLite、Prompt 或 Provider 原始响应。

## 13. 调用次数、限流与用量

每轮选择一种 Provider 方法：

```text
没有历史方案 -> extract_facts(...)
已有历史方案 -> continue_case(...)
```

不在同一轮继续调用 `polish_text(...)`，也不先抽取再续答。`before_call` 每个用户轮次执行一次；成功后 `after_call` 执行一次并把同一份 usage 写入 turn 和日用量。

现有 Provider 内部对瞬时错误的重试策略保持不变：一次逻辑 Provider 调用在失败时可能产生第二次 HTTP 尝试，但不会产生两个成功响应或重复记账。测试需要区分“Provider 方法调用次数”和“底层重试次数”。

失败轮次仍消耗一次本地日请求限额，但没有成功 usage 和 turn，保持现有行为。

## 14. 审计

审计阶段增加或明确以下可观察信息：

- 本轮选择 `extraction` 还是 `case_continuation`。
- 最终 `turn_kind`。
- 方案生成阶段实际使用的 citation refs。
- Provider 输出失败只记录安全错误分类，不记录 answer、Prompt 或原始 JSON。

普通 `followup_answer` 不伪造 rules、draft 和 rendering 成功事件；未执行的阶段不写成成功。`plan_update` 才记录完整规则、管辖、draft 和 rendering 阶段。

## 15. 测试设计

### 15.1 Provider 单元测试

- DeepSeek 续问请求使用 JSON mode、temperature 0 和受限上下文。
- 正常同案追问、事实变化、撤回事实、已注册新场景和未知新场景。
- 拒绝未知槽位、重复 refs、越界 action、越界 citation、URL、额外字段和非法字段组合。
- Provider identity、request id 和 usage 与事实抽取路径一致。
- Fake Provider 对同样输入确定性返回，且可注入续问结果与错误。

### 15.2 流水线回归

覆盖全部 9 个正式 Playbook（基础押金场景加 8 个已迁移场景）：

- 每个场景首次完整陈述都形成预期 plan。
- 每个场景随后发送普通追问，只返回 `followup_answer`，不重复 plan。
- 每个场景 citations 恰好等于自身 `legal_basis`，不含其他场景引用。

重点用例：

- “那不配合怎么办”“他不配合怎么办”等代词追问。
- 连续两次相似追问，每次可有针对性回答，但不复制完整方案。
- 补充金额、日期、证据状态等新事实后返回 `plan_update`。
- 更正已有事实和明确撤回已有事实。
- 从网购退货切换到租房押金，返回 `new_case` 且原 facts 不变。
- 一条消息混合两个纠纷时，以主要独立新纠纷分案，不自动合并。
- Provider 超时、非法 JSON、越界引用和数据库失败时保留前一方案。
- 每个用户轮次的 Provider 方法计数为 1，usage 只累计一次。
- 网购退货的普通追问绝不出现 `住房租赁条例`。

### 15.3 历史与 API

- 新五种 `turn_kind` 的响应组合校验。
- 新响应保存和恢复后完全一致。
- 无新字段的旧 fact collection、首份 plan、更新 plan 均可恢复。
- 连续旧重复 plan 在历史投影中变成紧凑回复。
- 旧无关 citation 被白名单过滤，相关结构化方案仍可读取。
- 短回复后，右栏使用最近一份 plan；更新方案后改用新 plan。
- 不向 API 返回 facts、Prompt、请求头、密钥或原始 Provider 内容。

### 15.4 浏览器验收

使用 Fake Provider 在桌面和手机视口检查：

- 初始方案、普通追问、方案更新和分案提示四种连续状态。
- 右栏在短回复后不变，在方案更新后同步变化。
- “作为新咨询继续”只预填、不自动发送。
- 历史刷新后不重复展开旧方案。
- 长中文回复、三个行动项和三个 citation 不溢出、不重叠。
- 360px 手机宽度和常用桌面宽度无横向滚动。

### 15.5 最小 DeepSeek 真实验证

自动化全部通过后，只执行一个两轮网购退货会话：

1. “网购商品与描述不符，商家拒绝退款”形成初始方案。
2. “他还是不配合怎么办”返回 `followup_answer`。

验证两个独立 DeepSeek request id、非零 token usage、第二轮 `plan == null`、回答与当前方案相关、引用是退货 Playbook 白名单子集，以及 DeepSeek Key 未出现在 API、日志或 SQLite 中。事实更新、新场景和错误路径由 Fake Provider 自动化覆盖，不额外消耗真实额度。

## 16. 验收标准

实现完成必须同时满足：

- 原复现会话的第二、第三轮不再返回重复完整方案。
- 普通续问可直接回答“下一步怎么做”，并只选择既有行动和法条。
- 新事实触发本地重算且界面明确标记方案更新。
- 新纠纷不污染当前案件，并可一键预填到新咨询。
- 方案和短回复中的 citation 全部属于当前 Playbook。
- 短回复后右侧完整方案仍然存在。
- 旧历史可读取，连续旧重复方案不再重复展开。
- Provider 失败后原方案、原 facts 和用户草稿保持不变。
- 全部自动化测试通过，桌面与手机浏览器验收通过。
- 最小 DeepSeek 两轮真实验证通过，且没有泄露密钥。

## 17. 实施边界

预计实现只触及以下既有边界：

- `app/agent/models.py`
- `app/agent/pipeline.py`
- `app/providers/base.py`
- `app/providers/deepseek.py`
- `app/providers/fake.py`
- `app/rendering/models.py` 或一个窄职责的法条加载模块
- `app/api/schemas.py`
- `app/history/service.py`
- `app/web/js/api.js`
- `app/web/js/render.js`
- `app/web/js/app.js`
- 对应的 pytest 与浏览器验收文件

不修改 `.env`、不读取或输出 DeepSeek Key、不改 SQLite 表结构，也不做与本问题无关的视觉重构。
