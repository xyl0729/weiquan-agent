# 验证码可选开关实施计划

## 目标

在不删除阿里云验证码集成的前提下，使验证码在所有发布阶段默认关闭，并允许以后通过单一配置开关重新启用。

## 任务 1：配置契约

文件：

- `app/config.py`
- `tests/test_config.py`
- `tests/test_rollout_gates.py`

步骤：

1. 增加默认关闭的 `captcha_enabled` 布尔配置。
2. 增加生产配置测试：关闭时场景配置可为空，开启时必须完整。
3. 保持验证码前缀格式校验和现有生产安全校验。

## 任务 2：后端验证与公开配置

文件：

- `app/integrations/captcha.py`
- `app/auth/dependencies.py`
- `app/api/auth.py`
- `app/api/schemas.py`
- `app/api/trial.py`
- `tests/test_auth_api.py`
- `tests/test_trial_api.py`
- `tests/test_aliyun_integrations.py`

步骤：

1. 增加不访问网络的禁用验证器。
2. 生产环境只在开关开启时实例化阿里云验证器。
3. 注册和首次试用允许省略验证码令牌；开启时仍由阿里云验证器拒绝缺失令牌。
4. 公开配置接口只在生产且开关开启时暴露场景信息。

## 任务 3：浏览器与 CSP

文件：

- `app/main.py`
- `app/web/js/api.js`
- `app/web/js/captcha.js`
- `app/web/js/auth.js`
- `app/web/js/privacy.js`
- `app/web/js/app.js`
- `tests/test_web_security.py`
- `tests/e2e/test_public_beta_flows.py`

步骤：

1. 关闭时不允许阿里云验证码 CSP 来源。
2. 前端关闭时不加载脚本、不显示控件、不提交占位令牌。
3. 首次试用隐私文案根据开关决定是否提及人机验证。
4. 保持开启时的现有弹窗验证流程。

## 任务 4：验证

1. 运行配置、认证、试用、集成和 Web 安全测试。
2. 运行 JavaScript 语法检查。
3. 运行完整测试集；网络集成继续使用 Fake 或 Stub。
4. 检查差异，确认未修改试用、IP、邮箱、配额和 DeepSeek 策略。
