# 本地完整测试身份实施计划

> 日期：2026-08-13
> 依据：[本地完整测试身份设计](../specs/2026-08-13-local-full-test-identity-design.md)
> 状态：实施中

## 目标与边界

让 `deployment_mode=local` 的浏览器获得由服务端明确授权的完整工作区身份，
无需注册或登录即可测试持久化咨询、续问、历史和附件。`test` 与 `production`
继续使用现有账号或匿名试用流程。

本次不增加模型选择器，不改变单个案件的续问轮次，不调用真实 DeepSeek，不部署到
生产服务器，也不修改生产数据库、VPN 或小程序。

## 1. 运行模式契约

**文件**

- 新增 `app/api/runtime.py`
- 修改 `app/api/schemas.py`
- 修改 `app/main.py`
- 新增 `tests/test_runtime_config.py`

**实施**

- 新增严格的 `RuntimeConfigResponse`，只允许
  `local_full_test` 与 `account` 两种身份模式。
- 新增 `GET /api/runtime-config`；只有 `local` 返回
  `local_full_test`，其余部署模式返回 `account`。
- 注册只读路由，不返回密钥、路径、Provider 配置或其他运行参数。

**验证**

- 三种部署模式返回正确的精简响应。
- 响应模型拒绝额外字段。

## 2. 前端身份与能力

**文件**

- 新增 `app/web/js/capabilities.js`
- 修改 `app/web/js/api.js`
- 修改 `app/web/js/app.js`
- 修改 `app/web/js/auth.js`
- 修改 `app/web/js/render.js`
- 修改 `app/web/styles.css`

**实施**

- 增加严格校验的 `getRuntimeConfig()`。
- 启动时先读取运行模式；本地模式直接激活 `local` 身份，不请求账号、试用或
  CSRF 接口；运行模式读取失败时关闭工作区能力。
- 通过 `hasWorkspaceAccess()` 统一允许 `local` 和 `authenticated` 使用咨询、
  续问、历史、删除、附件与刷新恢复。
- 保留 `hasRegisteredAccount()` 处理隐私确认、账号额度、登录与登出等账号专属逻辑。
- 本地咨询统一调用 `/api/consult` 并保留 `session_id`；匿名用户继续调用
  `/api/trial/consult`。

## 3. 界面状态与错误文案

**文件**

- 修改 `app/web/js/render.js`
- 修改 `app/web/styles.css`
- 修改 `app/web/js/api.js`

**实施**

- 本地身份显示“本地完整测试”和“不计应用额度”，并通过标题说明真实模型调用仍会
  产生 API 费用。
- 本地模式禁用账号按钮，隐藏匿名试用说明，显示历史、续问和附件控件。
- 为 `public_registration_closed` 增加明确中文提示。
- 保持桌面与移动端头部、工作区和编辑器布局稳定。

## 4. 回归与浏览器验证

**文件**

- 修改 `tests/test_web_security.py`
- 修改 `tests/e2e/test_public_beta_flows.py`
- 新增 `tests/e2e/test_local_full_test_flow.py`

**验证**

- JavaScript 语法检查。
- 运行模式、API、身份、历史、附件和网页契约的聚焦测试。
- 账号模式匿名试用和注册工作流不回归。
- Playwright 验证本地首轮、续问、历史、刷新恢复及桌面/移动端布局。
- 最后运行不依赖外部服务的完整测试套件，临时目录放在仓库外。
