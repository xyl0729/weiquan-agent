# 维权咨询 Web 界面实施计划

> 日期：2026-08-07
> 依据：[维权咨询 Web 界面设计](../specs/2026-08-07-consultation-web-interface-design.md)
> 状态：设计已确认，按本计划实施

## 目标

在不改变现有 `POST /api/consult` 决策职责的前提下，为本地维权
Agent 增加可恢复、可删除的咨询历史，以及由 FastAPI 同源托管的白色
咨询工作台。默认自动化测试继续使用 Fake Provider；真实 DeepSeek 只做
受控的本地冒烟测试。

## 1. 会话历史存储与公开模型

**文件**

- 修改 `app/db/models.py`
- 修改 `app/db/session.py`
- 新增 `app/history/__init__.py`
- 新增 `app/history/service.py`
- 修改 `tests/test_db.py`

**实施**

- 为 `SessionStore` 增加未过期且至少含一个 turn 的会话列表读取。
- 保持 `updated_at` 倒序和 UUID 稳定排序，避免同一时间下顺序漂移。
- 增加单会话删除方法，依赖 SQLite 外键级联删除 turns 与审计记录。
- 在历史服务中按首条用户消息生成标题：折叠空白，保留前 24 个
  Unicode 字符，超出后追加省略号，不调用模型。
- 详情读取时将持久化的公开响应重新通过 `ConsultResponse` 校验；任何
  损坏数据都以安全的数据完整性错误失败，不返回部分结果。

**验证**

- 覆盖列表排序、零 turn 过滤、过期过滤、Unicode 标题、详情恢复、
  删除级联和幂等删除。

## 2. 会话 API、同源页面与安全边界

**文件**

- 修改 `app/api/schemas.py`
- 新增 `app/api/sessions.py`
- 修改 `app/deps.py`
- 修改 `app/main.py`
- 修改 `tests/test_api.py`

**实施**

- 增加 `GET /api/sessions`、`GET /api/sessions/{id}` 和
  `DELETE /api/sessions/{id}`。
- 列表只返回标题、场景、状态和时间；详情只返回公开 session 信息、
  用户消息和重新校验后的 `ConsultResponse`。
- 会话未知或过期时返回稳定的 `session_not_found`；删除不存在的有效
  UUID 时仍返回 `204 No Content`。
- 复用已注入 Pipeline 的 `SessionStore`，避免测试和运行时连接到不同
  数据库。
- FastAPI 在 `/` 返回正式页面，在 `/static/*` 托管本地 CSS、JS 与图标。
- 为页面和静态资源增加仅允许同源脚本、样式、图片和连接的 CSP，并补充
  `nosniff`、禁止嵌入和安全来源策略。

**验证**

- 覆盖 API schema、排序、恢复、删除、敏感字段缺失、损坏响应、
  首页/静态资源和安全响应头。
- 回归现有 `/api/consult`、`/health`、限流与安全错误测试。

## 3. 白色咨询工作台

**文件**

- 新增 `app/web/index.html`
- 新增 `app/web/styles.css`
- 新增 `app/web/icons/*.svg`

**实施**

- 用语义化 HTML 建立页头、历史侧栏、新咨询页、案件对话区、案件摘要、
  错误提示和删除确认对话框。
- 使用白色主界面、浅冷灰侧栏、近黑正文，以及克制的蓝、朱红和黄色
  状态色；不使用渐变、装饰光斑、营销式 Hero 或嵌套卡片。
- 桌面端使用三栏案件工作台；中等宽度将历史变为抽屉；手机端按案件
  标题、对话、输入、摘要顺序单列排列。
- 使用稳定尺寸的图标按钮、本地 Lucide 风格图标、清晰焦点、可读状态
  文本和 `prefers-reduced-motion`。

**验证**

- 检查 1366x768、936x900、390x844 和 360x800，无水平溢出、文本裁切、
  控件重叠或不可达区域。

## 4. 前端数据流与完整交互

**文件**

- 新增 `app/web/js/api.js`
- 新增 `app/web/js/state.js`
- 新增 `app/web/js/render.js`
- 新增 `app/web/js/app.js`

**实施**

- `api.js` 只负责同源请求、公开响应结构校验和安全错误归一化。
- `state.js` 维护当前会话、历史、turns、草稿、请求状态和界面错误；
  `sessionStorage` 只保存当前会话 ID。
- `render.js` 只用 `createElement`、`textContent`、属性 API 和
  `replaceChildren` 渲染动态内容，不拼接用户输入到 `innerHTML`。
- `app.js` 负责启动时并行读取健康状态与历史、首轮咨询、后续追问、
  历史恢复、新建独立咨询、删除确认、重试和键盘提交。
- 首次成功响应后进入案件工作台；失败时保留草稿；请求中锁定发送动作。
- `need_more_facts`、`ready` 和 `escalate` 分别显示明确状态。案件摘要只
  重排用户消息、`questions`、`plan`、`verdict` 和 `citations`。
- 法条链接只接受后端已校验的 HTTP(S) 地址，并使用安全新窗口属性。

**验证**

- 用 Fake Provider 完成首次发送、追问、方案、独立新咨询、恢复、刷新和
  删除的浏览器流程。
- 检查空白、超长、重复点击、过期、429、503、500、断网和历史加载失败。

## 5. 回归、浏览器验收与 DeepSeek 冒烟

**文件**

- 按实际命令补充 `README.md`，不修改根目录 `PLAN.md` 和
  `新建 文本文档.txt`

**实施与验证**

1. 运行新增历史与 API 测试。
2. 运行完整离线 `pytest`。
3. 运行 `scripts/verify_refs.py` 与 `scripts/check_recall.py`，确认法律数据
   门禁不受界面改动影响。
4. 仅监听 `127.0.0.1` 启动本地服务，用浏览器完成桌面和手机截图验收。
5. 不读取或输出 `.env`，让应用自行加载用户已保存的 DeepSeek 配置，
   对“租房押金扣除”执行两轮真实冒烟。
6. 检查浏览器响应、应用输出和 SQLite 公开字段不包含 Key、
   Authorization Header、完整 Prompt 或 traceback。

每一部分完成后立即运行对应测试；失败先修复，再进入下一部分。
