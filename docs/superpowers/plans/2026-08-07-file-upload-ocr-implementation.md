# 文件上传与本地 OCR 实施计划

> 日期：2026-08-07
> 依据：[文件上传与本地 OCR 设计](../specs/2026-08-07-file-upload-ocr-design.md)
> 状态：设计已确认，实施计划待确认

## 目标

在现有新咨询、案件续问和历史恢复能力上增加 PDF、PNG、JPG/JPEG
附件。原文件只在项目内受控临时目录中存在，由本地解析器和 OCR 子进程
提取；用户核对并确认后的文字才可以作为独立证据上下文进入 Agent。

本阶段必须同时保持：

- 原始文件、临时路径和 OCR 内文不进入日志、审计或 DeepSeek 请求。
- 纯文字咨询完全向后兼容，每个用户轮次最多调用一次 Provider。
- 成功 turn、session 更新和附件绑定在同一 SQLite 事务中完成。
- 咨询失败或返回 `new_case` 时附件仍是可复用的 `confirmed` 草稿。
- 自动化和浏览器验收只使用 Fake Provider，不执行新的 DeepSeek POST。
- 不读取、输出或修改 `.env`，也不修改现有 `.env.example`。

实施开始前，先把当前案件续问阶段的改动验证并独立提交。OCR 改动按下列
任务逐项测试和提交，不能与前一阶段未提交代码混在同一个提交中。

## 1. 依赖、配置、领域模型与安全错误

**文件**

- 修改 `requirements.txt`
- 修改 `app/config.py`
- 新增 `app/attachments/__init__.py`
- 新增 `app/attachments/models.py`
- 新增 `app/attachments/errors.py`
- 修改 `app/agent/models.py`
- 修改 `tests/test_config.py`
- 新增 `tests/test_attachments_models.py`

**先写测试**

- `Settings` 为附件数量、字节、页数、像素、提取字符、确认字符、超时、
  草稿有效期和低置信度阈值提供设计中的默认值及上下界。
- 附件临时目录解析后必须位于项目根目录内，不能是静态目录、数据库文件
  所在文件本身或项目外路径。
- 文件名拒绝控制字符、空白名和超过 255 个 Unicode 字符的值。
- `ExtractionBlock`、`ExtractionResult`、`AttachmentReviewPublic`、
  `AttachmentTurnPublic` 和 `AttachmentEvidenceContext` 使用
  `extra="forbid"`；公开投影和证据上下文不可变。
- 附件状态只允许 `processing`、`review_required`、`confirmed`、
  `failed` 和 `bound`，并拒绝不符合状态的数据组合。
- 所有设计中的公开错误代码都有固定、安全且可操作的中文消息，不包含
  依赖名称、路径、异常文本或 OCR 内容。

**实施**

- 加入有界版本的 `python-multipart`、`pypdf`、`pypdfium2`、`Pillow`
  和 `rapidocr-onnxruntime`，保持 Python 3.11 兼容。
- 在 `Settings` 中加入附件默认限制和项目内临时目录；这些字段不要求
  用户填写新环境变量。
- 在附件领域模型中分离提取块、提取结果、核对接口公开对象、历史/turn
  公开对象和 Provider 证据对象。核对对象可以包含提取块，历史/turn
  对象只包含已确认文字和公开元数据；两者都不得含 SHA-256、本地路径、
  预留令牌或内部异常。
- 为输入错误、资源限制、状态冲突、附件不存在和 OCR 不可用建立窄错误
  类型，逐项实现并测试以下稳定代码：
  - 类型与内容：`attachment_type_unsupported`、
    `attachment_type_mismatch`、`attachment_name_invalid`、
    `attachment_pdf_encrypted`、`attachment_corrupt`、
    `attachment_text_empty`。
  - 资源限制：`attachment_too_large`、
    `attachment_page_limit_exceeded`、
    `attachment_pixel_limit_exceeded`、
    `attachment_extracted_text_too_long`、
    `attachment_extraction_timeout`。
  - 查询与状态：`attachment_not_found`、
    `attachment_not_reviewable`、`attachment_not_confirmed`、
    `attachment_already_bound`。
  - 本轮上下文：`attachment_count_exceeded`、
    `attachment_context_too_long`。
  - 能力降级：`attachment_service_unavailable`。
- 统一异常处理器把这些错误映射为 `413`、`404`、`409`、`422` 或
  `503`，不得把内部异常文本直接作为公开消息。
- `confirmed_text` 只做 Unicode 字符数校验和首尾空白处理，不静默截断。

**验证**

```powershell
pytest -q tests/test_config.py tests/test_attachments_models.py --basetemp=.test-tmp/attachments-models
```

## 2. SQLite v2 迁移、状态机与附件存储

**文件**

- 修改 `app/db/models.py`
- 修改 `app/db/session.py`
- 新增 `app/attachments/store.py`
- 修改 `tests/test_db.py`
- 新增 `tests/test_attachments_store.py`

**先写测试**

- 空数据库直接初始化为 schema v2；真实结构为 v1 的数据库只新增附件表
  和索引，不重建或改写 sessions、turns、audit、usage 和 rate limit。
- 迁移事务失败时 `PRAGMA user_version` 仍为 1；高于当前版本的数据库
  关闭失败，不能被降级覆盖。
- 覆盖设计中的全部合法状态转换，并拒绝失败后确认、未确认绑定、绑定后
  移动和已绑定删除。
- `review_required -> confirmed` 原子保存用户文字；再次编辑已确认附件
  时文字与更新时间在同一事务更新。
- 一组附件只能由一个随机 `reservation_id` 预留；重复 ID、并发预留、
  过期草稿、已有预留和已绑定附件均被拒绝。
- 释放预留后附件仍为 `confirmed`；绑定要求预留令牌匹配，并同时写入
  `session_id`、`turn_id`、`bound` 状态和清空预留字段。
- session 删除或过期时级联删除绑定附件；未绑定草稿按 1 小时有效期
  清理，其他 session 的附件不受影响。
- migration、清理和失败回滚后，原有会话历史逐字段保持不变。

**实施**

- 将 `SCHEMA_VERSION` 从 1 升为 2。`initialize()` 先读取
  `PRAGMA user_version`，再在单事务中执行对应迁移，成功后才写版本号。
- 新增 `attachments` 表及索引，包含设计字段，并加入
  `reservation_id`、`reserved_at` 和 `turn_position`。数据库约束保证：
  - 草稿没有 session/turn，`bound` 同时具有 session/turn。
  - `confirmed` 和 `bound` 具有非空 `confirmed_text`。
  - 一个 turn 可以有最多 3 个附件，但 `(turn_id, turn_position)` 唯一；
    单个附件行只能指向一个 turn。
  - 通过 `(session_id, turn_id)` 复合外键或等价数据库约束，保证 turn
    确实属于同一 session；session/turn 删除均级联。
  - 预留只存在于未绑定 `confirmed` 附件。
- SQL、连接和跨表事务继续由 `SessionStore` 掌管。`AttachmentStore`
  只作为窄领域 façade 调用这些方法，不能另开连接完成 turn 绑定。
- 提供创建 processing 草稿、保存结果/错误、读取、确认、删除、顺序预留、
  释放、按 turn 查询和有界清理方法。
- 为后续 Pipeline 增加单连接持久化入口，但本任务先只验证它能在任一步
  失败时回滚 session、turn 和附件三类写入。
- `_purge()` 同时处理过期 session 和附件草稿，并限制单次删除数量，
  避免请求路径执行无界清理。

**验证**

```powershell
pytest -q tests/test_db.py tests/test_attachments_store.py --basetemp=.test-tmp/attachments-store
```

## 3. PDF、图片提取器与固定夹具

**文件**

- 新增 `app/attachments/extractors.py`
- 新增 `tests/test_attachments_extractors.py`
- 新增 `tests/fixtures/attachments/`

**先写测试**

- 文字 PDF 保持页序并使用 `direct_text`，不初始化 OCR。
- 扫描 PDF 使用 OCR，混合 PDF 只渲染无足够可选文字的页并返回 `mixed`。
- 中文、金额、日期和数字 PNG/JPEG 返回有页码、段落顺序和
  `0..1` 置信度的结构化块。
- 模糊内容产生低置信度警告；纯空白或没有可核对文字的文件返回
  `attachment_text_empty`。
- 加密、损坏、截断、伪装类型、超 20 页、超 25MP、像素炸弹和超过
  200,000 字符均返回稳定错误代码。
- 扩展名和浏览器 MIME 不参与实际类型信任；PDF、PNG、JPEG 只按签名
  与成功解码结果认定。
- OCR 断言只检查关键中文/数字、页序和置信度范围，不依赖空格或标点
  完全一致。

**实施**

- 定义窄 `DocumentExtractor` 协议和可注入 OCR 引擎，使大多数单元测试
  可以确定性覆盖分支，并保留一个小型真实本地 OCR 冒烟夹具。
- PDF 先由 `pypdf` 检查结构、页数和加密状态；有足够可选文字的页直接
  提取，其余页才由 `pypdfium2` 以固定 DPI 渲染。
- 图片由 Pillow 校验签名、格式、尺寸和完整解码，去除元数据并转换到
  OCR 支持的 RGB/灰度模式。捕获并拒绝 decompression bomb 警告。
- RapidOCR 结果规范化为按页、按阅读顺序排列的块；缺失或非法置信度
  关闭失败，低于 `0.75` 的块加入公开警告代码。
- 合并结果前累计 Unicode 字符数，超限整体拒绝，不返回截断文本。
- 夹具必须为仓库自有的最小无个人信息材料，不能含真实合同、聊天记录
  或用户上传内容。

**验证**

```powershell
pytest -q tests/test_attachments_extractors.py --basetemp=.test-tmp/attachments-extractors
```

## 4. 隔离子进程、流式上传与临时清理

**文件**

- 新增 `app/attachments/worker.py`
- 新增 `app/attachments/service.py`
- 新增 `tests/test_attachments_service.py`

**先写测试**

- multipart 文件按块写入，超过 10 MiB 时立即停止并删除部分文件；内存
  中不会保留完整原文件。
- 文件名永不参与路径拼接，内部文件名为随机 UUID，且最终路径始终位于
  项目内受控临时目录。
- SHA-256、签名类型、实际字节数和数据库元数据一致。
- worker 只收到随机临时路径、结构化 job 和限制；测试环境中的
  `DEEPSEEK_API_KEY`、Authorization 及其他非白名单变量在子进程中不可见。
- 正常、解析错误、数据库错误、父进程取消、90 秒超时、终止和 worker
  非法输出路径都删除原始临时文件。
- 超时先终止再强制杀死 worker，数据库变为安全 `failed`，不遗留
  `processing` 记录或孤儿文件。
- 服务启动时把遗留 `processing` 记录改为失败，并删除没有活动记录的
  项目内临时文件；清理范围不能越过受控目录。
- 日志捕获断言不含文件正文、确认正文、完整文件名、路径、Prompt 或 Key。

**实施**

- 用 `python-multipart` 的流式回调直接消费 `Request.stream()`，只允许
  一个名为 `file` 的 part，拒绝额外字段、额外文件和畸形 multipart。
  不依赖 Starlette 自动生成的系统临时文件。
- 上传时同步执行字节限制、SHA-256 和文件头采样，写入配置的项目内
  私有目录；目录不能被 `/static` 或任何下载路由访问。
- 父进程创建 `processing` 草稿后，以白名单环境启动独立 worker。
  白名单只保留 Python/Windows 运行所需变量，并把 `TEMP`/`TMP` 指向
  受控目录，明确不复制 DeepSeek 或服务端密钥。
- worker 不接收数据库、session、HTTP 请求或用户文件名，只从随机 job
  文件读取路径和数值限制，写出有大小上限的结构化结果。
- 父进程验证 worker 退出码、结果 schema 和输出大小，再持久化
  `review_required` 或公开 `failed` 错误；内部异常只做类别映射。
- 所有原文件、job 和 result 文件在 `finally` 中删除。每次上传、读取和
  启动只执行有界孤儿清理。

**验证**

```powershell
pytest -q tests/test_attachments_service.py --basetemp=.test-tmp/attachments-service
```

## 5. 附件 API、依赖注入与 OCR 健康检查

**文件**

- 新增 `app/api/attachments.py`
- 修改 `app/api/schemas.py`
- 修改 `app/api/health.py`
- 修改 `app/deps.py`
- 修改 `app/main.py`
- 修改 `tests/test_api.py`
- 修改 `tests/test_deps.py`

**先写测试**

- `POST /api/attachments` 只接收单个允许文件并返回
  `review_required` 或安全 `failed` 公开对象。
- `GET` 返回核对所需的页块、置信度和警告，但不返回路径、SHA、
  reservation、原始字节、堆栈或内部依赖信息。
- `PATCH` 只允许 `review_required`/`confirmed`，拒绝空白和单附件越界
  文字；保存后返回最新 `confirmed_text`。
- `DELETE` 仅删除未绑定草稿并保持幂等；预留中返回冲突，已绑定附件
  不能从历史中单独删除。
- 未知、过期、非法 UUID、错误状态、超限和 OCR 不可用均使用统一安全
  错误结构及预期 HTTP 状态。
- CORS 允许同源 `PATCH`，安全响应头在四个附件方法上全部存在。
- `/health` 只探测本地依赖和 OCR 模型可加载，不处理用户文件、不联网；
  `ocr=unavailable` 只禁用附件功能，纯文字咨询仍被标记为可用。
- 依赖注入复用 Pipeline 的同一个 `SessionStore`/`AttachmentStore`，
  测试应用不会意外连接默认数据库。

**实施**

- 新增四个附件路由及严格公开 schema，将 multipart 解析交给
  `AttachmentService`，不在路由中复制文件处理逻辑。
- 扩展统一异常映射和 CORS 方法列表，保持现有错误响应结构。
- 在应用 state 中缓存 `AttachmentStore`、`AttachmentService` 和一次性
  OCR readiness 结果；健康检查不得在每次请求重新下载或重复初始化模型。
- 健康响应明确区分核心咨询能力与可选 OCR 能力。OCR 降级不能让现有
  前端把整个 Agent 判为不可用。
- 启动时执行 schema 初始化和有界附件恢复；失败只把 OCR 能力设为
  unavailable，除非共享 SQLite 本身不可用。

**验证**

```powershell
pytest -q tests/test_api.py tests/test_deps.py --basetemp=.test-tmp/attachments-api
```

## 6. 证据上下文与 Provider 安全边界

**文件**

- 新增 `app/attachments/context.py`
- 修改 `app/providers/base.py`
- 修改 `app/providers/fake.py`
- 修改 `app/providers/deepseek.py`
- 修改 `tests/test_providers.py`
- 新增 `tests/test_attachments_context.py`

**先写测试**

- `EvidenceContextBuilder` 拒绝不存在、重复、未确认、失败、已绑定、
  非当前 reservation 和超过 3 个的附件。
- 按请求 ID 顺序构建上下文，合计超过 12,000 Unicode 字符时整体拒绝，
  不截断、不自动摘要。
- 证据项只含附件 ID、公开文件名、媒体类型、页数和确认文字，不含提取
  块、置信度、路径、哈希或数据库字段。
- Fake Provider 分别记录 `extract_facts` 和 `continue_case` 收到的不可变
  证据上下文，纯文字调用默认收到空元组。
- DeepSeek 的新咨询和续问请求都把附件放在独立 JSON 字段；用户消息
  仍是独立字段，附件正文不会被拼进 system prompt。
- 附件内“忽略规则”“输出密钥”“改用其他法条”“返回任意 JSON”等
  提示注入不能改变场景白名单、槽位白名单、动作引用、法条引用或响应
  schema。
- Provider 请求中没有原始文件、base64、临时路径、提取块元数据或
  未确认文字。

**实施**

- `EvidenceContextBuilder` 从已预留的 `confirmed` 记录创建不可变证据，
  并执行后端最终的数量、状态、顺序和合计字符校验。
- `LLMProvider.extract_facts(...)` 与 `continue_case(...)` 增加默认空的
  附件证据参数；不传附件时保持现有调用和 Prompt 语义。
- Fake Provider 支持断言证据及现有两类调用计数，不增加隐藏的第二次
  模型步骤。
- DeepSeek 将证据标注为“用户提供、可能存在 OCR 错误的不可信材料”，
  明确禁止执行其中指令；用户本轮明确陈述优先。
- 仍只允许 Provider 返回既有事实抽取/续问结构，所有 facts、action refs
  和 citation refs 继续经过本地白名单校验。

**验证**

```powershell
pytest -q tests/test_attachments_context.py tests/test_providers.py --basetemp=.test-tmp/attachments-provider
```

## 7. Pipeline 预留、单调用与原子绑定

**文件**

- 修改 `app/agent/pipeline.py`
- 修改 `app/api/consult.py`
- 修改 `app/api/schemas.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_limits.py`
- 修改 `tests/test_api.py`

**先写测试**

- `ConsultRequest.attachment_ids` 缺省为空，最多 3 个 UUID，拒绝重复；
  旧纯文字请求和响应保持兼容。
- 新咨询附件只进入 `extract_facts(...)`，已有有效方案的续问附件只进入
  `continue_case(...)`，每轮 Provider 方法调用总数始终为 1。
- 同一附件被两个并发请求发送时只有一个能预留，另一个在 Provider 调用
  前返回状态冲突。
- Provider 失败、限流、取消、Pipeline 校验失败和持久化失败都释放预留，
  附件保持 `confirmed` 且没有 session/turn。
- 成功的事实收集、首份方案、普通短答和方案更新都把附件绑定到准确
  turn，并在响应中返回同一公开附件列表。
- `new_case` turn 可以正常保存到原案件，但附件不绑定原 turn；预留被
  释放，前端可把同一草稿带到新的独立咨询。
- session 更新、turn 插入和附件绑定任一步失败时全部回滚；不存在
  “响应失败但附件已绑定”或“turn 成功但附件仍可复用”。
- `turns.user_message` 只保存原始用户消息，不拼接 OCR 正文；审计和 usage
  不保存附件正文，调用计数和费用仍只累计一次。

**实施**

- 咨询入口先规范化附件 ID，再由 `AttachmentStore` 以随机 request
  reservation 原子预留整组附件。
- 使用 `EvidenceContextBuilder` 构建证据后，沿用现有首次咨询/案件续问
  分支，并只把证据传入该分支原本唯一的 Provider 方法。
- 在 `finally`/异常路径按 reservation 释放；释放操作必须具备幂等性，
  且不能影响已经成功绑定的附件。
- `new_case` 明确走“保存本轮响应、释放附件、不绑定”的路径。
- 重构 `_persist_success` 使用 `SessionStore` 的单连接事务，同时完成
  session 字段更新、turn 写入、附件 reservation 校验与绑定；无附件时
  仍走同一可靠事务入口。
- `PipelineResult` 和 `ConsultResponse` 增加本轮附件公开列表。公开响应
  只从提交后的数据库记录构建，不能相信请求中的 ID 或前端状态。

**验证**

```powershell
pytest -q tests/test_pipeline.py tests/test_limits.py tests/test_api.py --basetemp=.test-tmp/attachments-pipeline
```

## 8. 历史投影、级联清理与敏感数据边界

**文件**

- 修改 `app/history/service.py`
- 修改 `app/api/schemas.py`
- 修改 `tests/test_history.py`
- 修改 `tests/test_db.py`

**先写测试**

- 每个历史 turn 只返回绑定到该 turn 的附件，并保留请求顺序。
- 历史附件只含公开文件名、类型、大小、页数、方法、警告和
  `confirmed_text`；不返回未确认提取结果或内部字段。
- 不含附件字段的旧 `response_json` 仍能通过兼容投影恢复，返回空列表，
  且不改写原数据库。
- 绑定附件的 session 删除和 72 小时过期都级联清理；其他 session、
  未绑定草稿和原有 turn 不受影响。
- 草稿清理、历史读取和 session 删除并发时不串案、不返回半条记录。
- 注入损坏附件 JSON、跨 session turn 或非法状态时历史详情以安全数据
  完整性错误关闭失败，不返回部分历史。
- 数据库、审计、日志和公开响应扫描确认：OCR 正文只存在于附件表允许
  的 `confirmed_text`/提取结果列，不出现在 user message、audit、
  usage、rate limit 或错误信息中。

**实施**

- `SessionStore.get_session_history()` 在同一连接读取 turn 及其附件，
  `SessionHistoryService` 使用统一公开投影构建 `SessionTurnResponse`。
- 旧 turn 响应继续由既有兼容模型处理；附件列表来自关系表而非旧
  `response_json`，避免重复持久化正文。
- 保持数据库外键级联为绑定附件生命周期的唯一删除机制；未绑定草稿
  由有界清理处理。
- 对提取 JSON 和公开附件重新执行严格模型校验，任何损坏都映射为既有
  安全完整性错误。

**验证**

```powershell
pytest -q tests/test_history.py tests/test_db.py --basetemp=.test-tmp/attachments-history
```

## 9. 前端上传、核对、恢复与分案携带

**文件**

- 修改 `app/web/index.html`
- 修改 `app/web/js/api.js`
- 修改 `app/web/js/state.js`
- 修改 `app/web/js/render.js`
- 修改 `app/web/js/app.js`
- 修改 `app/web/styles.css`
- 修改 `tests/test_api.py`

**先写检查**

- 静态脚本仍不含 `innerHTML`、`insertAdjacentHTML` 或 `localStorage`，
  文件名和 OCR 文字只通过 `textContent`、`value` 和安全属性 API 渲染。
- API 校验器拒绝缺字段、未知状态、越界置信度、非法 URL/路径字段和
  非法咨询附件投影。
- 前端字符计数按 Unicode code point 计算，与后端 12,000 字符规则一致。
- 刷新恢复只从 `sessionStorage` 读取未绑定附件 UUID，再逐个 GET；
  storage 中不得出现文件名、OCR 正文或完整 API 响应。

**实施**

- 在新咨询和案件续问输入区加入本地 Lucide 回形针按钮、隐藏文件选择器
  和紧凑附件列表；按钮有可访问名称和 tooltip，不改变现有白色工作台。
- 一次可选最多 3 个允许文件，前端按顺序逐个上传。每项稳定显示文件名、
  类型、大小、页数、状态、警告及核对/移除/重新选择操作。
- 使用原生 `<dialog>` 构建核对窗口。按页显示提取块，低置信度段落使用
  现有黄色提示；编辑后合并为顺序稳定的 `confirmed_text` 并 PATCH。
- 对话框提供明确标题、关闭按钮、焦点返回和手机全宽布局；发送期间禁止
  编辑、删除、上传和重复提交。
- 任一附件为 processing、review_required 或 failed 时禁用发送，并显示
  具体原因。仍要求用户消息非空且不超过现有 4,000 字限制。
- 成功响应从草稿列表移除 bound 附件并显示在对应用户消息下；咨询失败
  保留消息和 confirmed 附件，不重复 OCR。
- `new_case` 的“作为新咨询继续”同时预填原消息并把 confirmed 附件 ID
  移到新咨询草稿，不自动发送、不绑定原案件。
- `sessionStorage` 用 `{new: [...], sessions: {sessionId: [...]}}` 形状
  分组保存未绑定 ID。启动、刷新和历史切换时只 GET 当前分组；404/过期
  项安全移除，其他案件的草稿不串入。storage 中仍不保存任何 OCR 正文。
- 健康检查单独控制回形针按钮。OCR unavailable 时显示简短可恢复提示，
  文本框、发送、历史和删除仍可用。
- CSS 使用不嵌套卡片的列表、8px 以内圆角和稳定行高；核对窗口、长中文
  文件名、3 个附件和警告在 360px、桌面及宽屏均不溢出或遮挡。

**验证**

```powershell
pytest -q tests/test_api.py --basetemp=.test-tmp/attachments-web
node --check app/web/js/api.js
node --check app/web/js/state.js
node --check app/web/js/render.js
node --check app/web/js/app.js
```

## 10. 全量回归、安全扫描与浏览器验收

**文件**

- 按失败用例最小范围修正相关模块
- 按实际安装和运行方式更新 `README.md`
- 不修改 `.env` 或 `.env.example`

**自动化验证**

1. 在项目虚拟环境安装有界依赖，确认 RapidOCR 模型已随本地依赖准备，
   启动或测试期间没有网络下载。
2. 运行全量测试、法条引用门禁和召回门禁。
3. 扫描前端危险 DOM API、敏感字段、临时路径和不应出现的 OCR 正文。
4. 检查项目临时目录在正常、失败和超时测试后为空。
5. 检查补丁格式及提交文件，不纳入用户已有 `.env.example` 改动。

```powershell
pytest -q --basetemp=.test-tmp/full-ocr
python scripts/verify_refs.py
python scripts/check_recall.py
rg -n "innerHTML|insertAdjacentHTML|localStorage" app/web tests
git diff --check
git status --short
```

**Fake Provider 浏览器验收**

- 仅监听 `127.0.0.1` 启动本地服务，不读取或展示 `.env`。
- 新咨询上传文字 PDF，核对、修改并确认后发送，事实抽取只调用 Fake
  `extract_facts` 一次。
- 已有案件上传扫描图片，分别验证普通短答与 `plan_update`，每轮只调用
  Fake `continue_case` 一次。
- 验证低置信度、空白、损坏、超限、删除、上传失败、咨询失败重试和
  刷新恢复。
- 验证 `new_case` 只把消息和 confirmed 附件带到新咨询，点击前不发送，
  原案件历史没有错误绑定。
- 删除/过期 session 后历史与附件同步消失；未绑定草稿按期清理。
- 在 1366x768、936x900、390x844 和 360x800 检查 3 个长文件名、核对
  对话框、键盘焦点、状态播报、无水平滚动、无遮挡和布局跳动。
- 检查网络面板没有文件或 OCR 请求发往外部域名，Fake 验收的 DeepSeek
  请求数为 0。

本阶段完成后停止，不执行真实 DeepSeek 附件咨询。只有用户另行明确批准，
才设计一次最小真实冒烟；该调用不属于自动化测试。

## 完成标准

- PDF、PNG、JPG/JPEG 可以在本地稳定提取，OCR 模型运行时无需联网。
- 未经用户确认的文字不能进入 Agent，原始文件在全部分支都已删除。
- worker 不继承 DeepSeek Key，日志、审计和错误响应不泄露附件正文。
- 每轮只调用一个 Provider 方法，纯文字咨询行为和费用计数不回归。
- turn、session 和附件原子提交；失败、重试和 `new_case` 不误绑定。
- schema v1 安全升级到 v2，旧历史不变，附件历史不串 session/turn。
- 前端上传、核对、恢复、移除、失败重试和分案携带在桌面及手机可用。
- 全量自动化、引用门禁、召回门禁、安全扫描和 Fake 浏览器验收全部通过。
- `.env`、`.env.example` 和真实 DeepSeek 额度均未被本阶段触碰。
