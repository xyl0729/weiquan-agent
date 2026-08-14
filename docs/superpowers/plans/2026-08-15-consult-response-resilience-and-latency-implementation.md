# 咨询回复韧性与延迟实施计划

> 日期：2026-08-15
> 依据：[咨询回复韧性与延迟设计](../specs/2026-08-15-consult-response-resilience-and-latency-design.md)
> 状态：实施中

## 目标与边界

修复前端误拒绝后端安全澄清的问题，把 DeepSeek 网络重试和格式纠正纳入 30 秒
统一预算，并让所有 Provider 故障返回联系当前问题与历史上下文的本地安全回复。
咨询页面最多等待 35 秒，降级回复不扣额度。

本次不修改法律规则、数据库结构、注册邮件、附件流程或配额上限，只部署
`weiquan-app`，不重启 PostgreSQL，不触碰独立音频项目。

## 1. 锁定前后端响应契约

**文件**

- 修改 `app/web/js/api.js`
- 修改 `tests/test_api.py`
- 修改 `tests/test_web_security.py`
- 修改相关 Playwright 测试

**实施**

- 将 `fact_collection + unverified_guidance` 定义为唯一允许的安全澄清例外。
- 保持 plan、verdict、reply、guidance、questions 和 limitations 的现有字段约束。
- 为合法例外和相邻非法组合增加成对契约样例。
- 浏览器仍拒绝真正非法的响应，不放宽其他轮次。

**验证**

- 后端合法安全澄清可被前端接受并展示。
- 非法非正式覆盖、混入方案字段或空澄清仍触发完整性错误。

## 2. DeepSeek 30 秒统一预算

**文件**

- 修改 `app/config.py`
- 修改 `.env.example`
- 修改 `app/deps.py`
- 修改 `app/providers/deepseek.py`
- 修改 `tests/test_config.py`
- 修改 `tests/test_providers.py`
- 修改 `tests/test_bounded_execution.py`

**实施**

- 新增 `LLM_TOTAL_TIMEOUT_SECONDS=30` 配置并由 Provider 工厂注入。
- 有执行器时向 `BoundedExecutor.run()` 传入总预算。
- 无执行器时使用同等异步超时边界。
- 将首次请求、网络重试、退避和结构化格式纠正包在同一次计时中。
- 超时取消底层任务、释放并发槽位，并统一映射为 `provider_timeout`。
- 正常快速响应立即返回，健康指标只记录一次最终结果。

**验证**

- 阻塞请求在预算内结束，不再叠加到 60 至 90 秒。
- 网络重试和格式纠正不能各自重新获得完整预算。
- 取消后执行器 running、waiting 计数恢复，下一请求可正常执行。

## 3. 上下文相关的 Provider 故障降级

**文件**

- 修改 `app/agent/pipeline.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_api.py`

**实施**

- 将 `ProviderOutputError` 专用分支扩展为会话已建立后的统一
  `ProviderError` 降级分支。
- 将真实 Provider 错误类别写入降级审计。
- 紧急风险继续优先使用本地紧急指导。
- 正式 Playbook 依据已有事实询问首个缺失必填项；事实完整时询问要继续核对的
  既有方案步骤或诉求。
- 已有未核验主题优先保留历史主题，并使用 `GuidanceBuilder` 的主题问题。
- 没有历史主题时才使用当前原文的本地主题识别，最后才使用通用澄清。
- 降级轮次保留现有 session 状态，持久化后使用 `consume_quota=False` 退款。

**验证**

- invalid output、timeout、network、busy、限流、拒绝和配置错误均返回安全回复。
- 正式案件、未核验主题、紧急风险和未知主题分别命中正确优先级。
- 当前消息和最近历史仍进入正常 DeepSeek 调用，降级不切换已有案件主题。
- 正常模型结果消费一次额度，所有降级结果退款。

## 4. 浏览器 35 秒等待保护

**文件**

- 修改 `app/web/js/api.js`
- 按需修改 `app/web/js/app.js`
- 修改 `tests/test_web_security.py`
- 修改 `tests/e2e/test_public_beta_flows.py`

**实施**

- 仅为 `/api/consult` 和 `/api/trial/consult` 增加 35 秒
  `AbortController` 保护。
- 为超时定义独立错误码和中文提示，不误报为离线。
- 在成功、失败、取消和超时路径都清理计时器。
- 保持 generation 防护，确保迟到响应不能覆盖新页面状态。
- 超时后结束 `busy.consult`，保留再次提问入口，不在前端自行扣减配额。

**验证**

- 模拟永不返回的咨询请求时，页面在 35 秒内停止加载。
- 正常和降级响应能在保护时间内显示。
- 附件、登录、验证码等其他请求不受咨询超时影响。

## 5. 自动化与本地验收

**验证顺序**

1. 配置、BoundedExecutor 和 DeepSeek Provider 聚焦测试。
2. Pipeline、API schema、配额和前端契约测试。
3. JavaScript 语法检查与 Python 编译检查。
4. 不依赖外部服务的完整 pytest 套件。
5. Playwright 桌面视口和 `360x800` 手机视口。
6. 本地真实 DeepSeek 首问、上下文追问和一次受控降级。
7. `git diff --check` 与变更范围复核。

测试临时目录放在仓库外。故障测试只使用 Fake Provider 或注入式 HTTP client，
不得消耗真实 DeepSeek 额度。

## 6. 仅应用容器发布

**实施**

- 重启本地服务并先让本地页面通过验收。
- 构建不可变镜像 `weiquan-agent:2026.08.15-2`。
- 使用现有生产预检和部署脚本，只替换 `weiquan-app`。
- 保留 `/etc/weiquan/weiquan.env`，设置 30 秒 Provider 总预算。
- 不执行数据库迁移，不重启或重建 `weiquan-postgres`。

**线上验证**

- 检查容器健康、`/live`、`/ready`、内部指标和公网 HTTPS。
- 验证静态文件哈希与新镜像标签。
- 使用无敏感信息的首问和追问确认 DeepSeek 联系上下文。
- 受控验证降级回复可显示、没有完整性错误且配额为 refunded。
- 确认页面不再出现超过 35 秒的持续加载。

## 完成标准

- 每次咨询和追问均进入携带上下文的 DeepSeek 路径。
- DeepSeek 整次逻辑调用最多使用 30 秒预算。
- Provider 故障仍返回针对当前纠纷的安全回复且不扣额度。
- 合法安全澄清可在桌面和手机浏览器显示。
- 页面加载不超过 35 秒。
- 本地与公网测试通过，只有 `weiquan-app` 被重新部署。
