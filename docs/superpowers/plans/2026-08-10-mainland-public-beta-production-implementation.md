# 中国大陆百人受控公测生产化实施计划

> 日期：2026-08-10
> 依据：[中国大陆百人受控公测生产化设计](../specs/2026-08-10-mainland-public-beta-production-design.md)
> 状态：设计已确认，实施计划待用户确认

## 目标与边界

在保留现有正式 Playbook、智能路由、附件 OCR、案件续问和本地已核验法条库的前提下，
把当前本地演示应用改造成可在中国大陆向最多 100 个注册账号开放的受控公测服务。

本计划必须同时满足：

- 匿名用户只有纯文字试用，累计最多 5 次 DeepSeek 逻辑调用，不保存咨询或历史。
- 注册用户每天最多 10 次、每月最多 50 次，按北京时间计算并支持失败退还。
- 生产咨询、账号、会话、配额和元数据只写 PostgreSQL；`data/statutes.db` 保持只读。
- 所有注册用户资源由服务端会话派生的 `user_id` 约束，不接受客户端所有者字段。
- 原始附件和未确认 OCR 文字只在私有临时目录短暂存在；只有确认文字可以持久化。
- 公开用户只能调用 DeepSeek，`FakeProvider` 只用于本地测试依赖注入。
- 一台 2 核、2 GiB ECS 同时承载维权 Agent 和小程序音频，但使用独立子域名和资源边界。
- 未完成 ICP、HTTPS、邮件、验证码、恢复演练和经用户批准的真实 DeepSeek 冒烟前，
  不开放公网注册。

实施期间不读取、输出或修改秘密配置文件及其示例，不访问受保护的测试临时目录，
不迁移本地演示咨询，不停止当前 `8000` 端口服务，也不执行真实 DeepSeek 请求。
测试临时目录统一显式放在 `D:\agent development\pytest-temp-production-*`。

## 实施规则

1. 每项任务先提交失败测试，再写满足测试的最小实现。
2. PostgreSQL 并发语义必须由真实 PostgreSQL 集成测试证明，不能用 SQLite 代替。
3. 默认测试使用 Fake DirectMail、Fake CAPTCHA、Fake OSS 和 Fake Provider，禁止外网。
4. 每项任务独立验证并独立提交；不得纳入工作区中已有的其他未提交改动。
5. 数据迁移优先保持向前兼容；生产回滚默认切回镜像，只有不可向前修复时才恢复备份。

## 1. 依赖、生产配置与 PostgreSQL/Alembic 基础

**文件**

- 修改 `requirements.txt`
- 新增 `requirements-dev.txt`
- 修改 `app/config.py`
- 新增 `app/db/engine.py`
- 新增 `app/db/tables.py`
- 新增 `alembic.ini`
- 新增 `migrations/env.py`
- 新增 `migrations/script.py.mako`
- 新增 `migrations/versions/__init__.py`
- 新增 `deploy/compose.test.yml`
- 修改 `tests/test_config.py`
- 新增 `tests/test_db_engine.py`
- 新增 `tests/integration/test_migrations.py`

**先写测试**

- `local`、`test`、`production` 三种配置有明确边界；只有生产模式强制 PostgreSQL。
- 生产模式缺少数据库 URL、外部基准 URL、会话密钥、IP HMAC 密钥、邮件、验证码或
  删除清单加密配置时启动失败，错误只包含字段名，不包含字段值。
- 生产模式拒绝 SQLite 业务库、HTTP 公网基准 URL、通配 CORS、不安全 Cookie、
  `fake` 默认 Provider 和过短随机密钥。
- 法条库路径必须是现有只读 SQLite 文件；附件目录必须与静态目录、日志目录和备份
  目录隔离。
- PostgreSQL Engine 使用 `pool_size=5`、`max_overflow=2`、连接预检和 UTC 时区；
  数据库暂时不可用时映射为安全的存储错误。
- Alembic 可以在空 PostgreSQL 上执行 `upgrade head`，重复执行不改数据；
  迁移版本高于应用支持范围时就绪检查失败。

**实施**

- 显式加入 SQLAlchemy 2、psycopg 3、Alembic、Argon2、邮箱校验、加密库及阿里云
  DirectMail/CAPTCHA SDK；把 pytest、Playwright、依赖审计等仅测试依赖移入
  `requirements-dev.txt`。
- `Settings` 增加部署模式、PostgreSQL、公开 URL、Cookie、隐私版本、DirectMail、
  CAPTCHA、OSS、配额、并发和保留期字段；Secret 继续使用 `SecretStr`。
- 生产应用只通过仓库外的受控环境取得秘密；仓库内不新增秘密模板或实际值。
- `app/db/engine.py` 只负责 Engine、连接池和事务入口，业务 SQL 不进入该模块。
- `app/db/tables.py` 作为 PostgreSQL 表元数据的唯一声明位置，后续迁移按任务增加。
- 测试 Compose 只暴露隔离的本地测试 PostgreSQL，不复用生产卷或本地演示数据库。

**验证**

```powershell
docker compose -f deploy/compose.test.yml up -d postgres-test
.\.venv\Scripts\python.exe -m pytest -q tests/test_config.py tests/test_db_engine.py tests/integration/test_migrations.py --basetemp='D:\agent development\pytest-temp-production-db-foundation'
.\.venv\Scripts\python.exe -m alembic check
```

## 2. 持久化契约、所有者字段与原子咨询事务

**文件**

- 新增 `app/db/contracts.py`
- 新增 `app/db/postgres.py`
- 修改 `app/db/models.py`
- 修改 `app/db/session.py`
- 修改 `app/attachments/models.py`
- 新增 `app/attachments/drafts.py`
- 修改 `app/attachments/store.py`
- 修改 `app/attachments/service.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/deps.py`
- 新增 `migrations/versions/20260810_0001_identity_consultation_core.py`
- 修改 `tests/test_db.py`
- 新增 `tests/test_db_contracts.py`
- 新增 `tests/integration/test_postgres_store.py`
- 修改 `tests/test_attachments_store.py`
- 修改 `tests/test_attachments_service.py`
- 修改 `tests/test_pipeline.py`

**先写测试**

- SQLite 开发适配器和 PostgreSQL 生产适配器通过同一套持久化契约测试。
- `SessionStore.persist_session_turn()` 不再接收 `sqlite3.Connection` 回调；
  session 更新、turn 写入和附件绑定由一个类型化 Unit of Work 原子完成。
- 任一写入步骤失败时 session、turn 和附件全部回滚；成功后附件不能再次预留。
- 每个 session、turn、附件和内容审计记录都有非空所有者，查询同时使用资源 ID 和
  所有者 ID。
- SQLite v2 本地库升级后，旧数据归属于固定的本地开发身份且逐字段可读；这些数据
  不进入 PostgreSQL。
- PostgreSQL 空库包含用户、咨询、turn、附件元数据和内容审计表的外键、唯一约束和
  所有权索引。
- 注册用户每轮最多上传 3 个 PDF、PNG 或 JPEG；单文件不超过 10 MiB、PDF 不超过
  20 页、最终确认的附件上下文合计不超过 12,000 字符，扩展名、声明 MIME 和文件
  签名必须一致。
- 未确认 OCR 块不写 PostgreSQL；应用重启、确认、删除、超时和异常都会删除临时
  OCR sidecar。

**实施**

- 在 `app/db/contracts.py` 定义窄的查询仓库和 `ConsultationUnitOfWork`，使用
  Pydantic/dataclass 命令传递所有者、session 更新、turn 和附件预留信息。
- 保留 `SessionStore` 作为本地开发适配器，但将 schema 升至 v3 并移除附件模块对
  SQLite 连接对象的认知。
- `PostgresApplicationStore` 使用 SQLAlchemy Core 和显式事务；不把 ORM Session
  或数据库连接泄露给 Pipeline、历史服务或附件服务。
- 生产 session 以 30 天保留期为准，不再沿用 72 小时本地演示语义。
- 原始附件处理后立即删除；未确认 OCR 块写入权限为 `0600` 的随机临时 sidecar，
  数据库只保存状态和公开元数据。确认时原子写入确认文字并删除 sidecar。
- 临时目录不挂入备份卷，不由 Nginx 提供，也不接受用户文件名参与路径。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_db.py tests/test_db_contracts.py tests/test_attachments_store.py tests/test_attachments_service.py tests/test_pipeline.py tests/integration/test_postgres_store.py --basetemp='D:\agent development\pytest-temp-production-uow'
```

## 3. 注册、邮箱验证、登录会话、CAPTCHA、CSRF 与隐私

**文件**

- 新增 `app/auth/__init__.py`
- 新增 `app/auth/models.py`
- 新增 `app/auth/passwords.py`
- 新增 `app/auth/tokens.py`
- 新增 `app/auth/service.py`
- 新增 `app/auth/dependencies.py`
- 新增 `app/privacy/__init__.py`
- 新增 `app/privacy/policy.py`
- 新增 `app/privacy/policy.zh-CN.txt`
- 新增 `app/integrations/__init__.py`
- 新增 `app/integrations/directmail.py`
- 新增 `app/integrations/captcha.py`
- 新增 `app/api/auth.py`
- 新增 `app/api/privacy.py`
- 修改 `app/api/schemas.py`
- 修改 `app/main.py`
- 修改 `app/deps.py`
- 新增 `scripts/create_admin.py`
- 新增 `scripts/invite_user.py`
- 新增 `migrations/versions/20260810_0002_auth_privacy.py`
- 新增 `tests/test_auth_passwords.py`
- 新增 `tests/test_auth_tokens.py`
- 新增 `tests/test_auth_service.py`
- 新增 `tests/test_auth_api.py`
- 新增 `tests/test_privacy.py`
- 新增 `tests/test_aliyun_integrations.py`
- 新增 `tests/integration/test_registration_capacity.py`

**先写测试**

- 邮箱经校验、去空白和大小写规范化后全局唯一；重复注册不重复占位。
- 并发注册时第 101 个有效占位无法创建；未过期待验证、正常和停用账号占位，
  删除及超过 24 小时的待验证账号不占位。
- 密码只允许 10 至 128 字符，使用 Argon2id 哈希并支持参数升级；明文不进入模型、
  数据库或日志。
- 邮箱验证和密码重置令牌随机强度至少 256 位、只存摘要、30 分钟过期、只能使用
  一次；重发使旧令牌失效。
- DirectMail 发送失败保留待验证账号并允许受限重发；容量已满时不调用邮件服务。
- 只有已验证且未停用账号可登录；错误文案不暴露邮箱是否存在。
- 登录会话只存 Cookie 标识摘要，7 天过期；登录、改密和重置后轮换或吊销会话。
- Cookie 具有 `Secure`、`HttpOnly`、`SameSite=Lax`；所有登录后状态写请求同时校验
  CSRF token 和同源 `Origin`/`Referer`。
- 注册、登录、验证重发和密码重置各自限流，限流键使用不可逆 HMAC。
- 注册和首次咨询分别记录隐私版本；版本变化后必须重新确认。

**实施**

- 使用 PostgreSQL 容量单例行和 `SELECT ... FOR UPDATE` 串行化容量检查。
- 增加用户、容量、认证会话、一次性令牌、隐私接受和安全限流表。
- 认证 Cookie 与 CSRF token 分离；CSRF token 只通过同源认证上下文接口返回并保留
  在页面内存，不写 `localStorage`。
- DirectMail 和 CAPTCHA 通过窄协议注入；SDK 异常只映射为稳定错误类别。
- 邮件验证与重置链接把令牌放在 URL fragment 中，避免令牌进入 Nginx 请求日志。
- `create_admin.py` 只在服务器受控终端运行；`invite_user.py` 用于内部和 10 人阶段，
  同样遵守容量、验证令牌和邮件发送规则。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_auth_passwords.py tests/test_auth_tokens.py tests/test_auth_service.py tests/test_auth_api.py tests/test_privacy.py tests/test_aliyun_integrations.py tests/integration/test_registration_capacity.py --basetemp='D:\agent development\pytest-temp-production-auth'
```

## 4. 服务端身份派生与跨用户隔离

**文件**

- 新增 `app/auth/principal.py`
- 修改 `app/auth/dependencies.py`
- 修改 `app/api/consult.py`
- 修改 `app/api/sessions.py`
- 修改 `app/api/attachments.py`
- 修改 `app/api/schemas.py`
- 修改 `app/history/service.py`
- 修改 `app/attachments/context.py`
- 修改 `app/attachments/store.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/deps.py`
- 修改 `tests/test_api.py`
- 修改 `tests/test_history.py`
- 修改 `tests/test_attachments_context.py`
- 新增 `tests/test_ownership.py`
- 新增 `tests/integration/test_cross_user_isolation.py`

**先写测试**

- `user_id`、owner 或 role 出现在公开请求体时被严格 Schema 拒绝。
- 服务端只从认证会话得到 `RegisteredPrincipal`，不能通过 Cookie 以外的用户输入
  更换身份。
- 用户 A 不能列出、读取、继续、删除、上传到或确认用户 B 的任何资源。
- 跨用户和随机不存在资源使用同一安全响应，标题、状态和数量均不可探测。
- 停用账号的全部会话立即失效，不能继续咨询或上传，但受控删除流程仍可使用。
- 附件预留、确认和绑定在最终提交前再次验证所有者，防止检查后替换。
- 管理员角色不会绕过咨询正文的所有权限制。

**实施**

- 所有历史、咨询和附件服务方法显式接收 `owner_id`，数据库查询同时包含主键和
  `owner_id`。
- 登录后的 `POST /api/consult` 只处理注册用户；匿名试用使用独立端点和数据流。
- 附件 API 要求注册身份，匿名请求统一返回 `registration_required`。
- 本地开发模式使用固定 `LocalPrincipal` 维持离线 Fake 测试，生产模式禁止该身份。
- 停用用户时在同一事务更新状态并吊销全部认证会话。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ownership.py tests/test_api.py tests/test_history.py tests/test_attachments_context.py tests/integration/test_cross_user_isolation.py --basetemp='D:\agent development\pytest-temp-production-ownership'
```

## 5. 匿名身份与日/月/累计原子配额

**文件**

- 新增 `app/trial/__init__.py`
- 新增 `app/trial/models.py`
- 新增 `app/trial/identity.py`
- 新增 `app/trial/service.py`
- 新增 `app/security/network.py`
- 新增 `app/limits/quota.py`
- 新增 `app/limits/reservations.py`
- 修改 `app/limits/usage.py`
- 修改 `app/limits/rate_limit.py`
- 新增 `app/api/trial.py`
- 修改 `app/api/consult.py`
- 修改 `app/api/schemas.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/main.py`
- 新增 `migrations/versions/20260810_0003_trial_quota.py`
- 新增 `tests/test_trial_identity.py`
- 新增 `tests/test_trial_api.py`
- 新增 `tests/test_quota.py`
- 新增 `tests/test_network_identity.py`
- 新增 `tests/integration/test_quota_concurrency.py`

**先写测试**

- 匿名身份只在首次 CAPTCHA 和隐私确认成功后创建；Cookie 随机、不可篡改、只存摘要，
  有效期固定为 365 天。
- 只信任来自回环 Nginx 的代理地址头；公网客户端伪造 `X-Forwarded-For` 不生效。
- 原始 IP 不入库；规范化 IP 通过独立密钥 HMAC，摘要 30 天后删除。
- 同一 IP 摘要 30 天最多创建 3 个试用身份；删除 Cookie 后第 4 次不能重新领取。
- 匿名前 5 次成功、第 6 次拒绝；全站北京时间同一天第 51 次拒绝。
- 注册用户北京时间同一天前 10 次、同月前 50 次成功，跨午夜和跨月边界正确。
- 匿名用量与注册用量完全分离；登录或注册不会迁移匿名历史或扣减注册额度。
- 并发请求不会超发；预留、成功、退还都是幂等状态转换。
- Provider 最终失败、排队拒绝或进入 Provider 前失败会退还；成功后历史保存失败仍
  计入额度并返回“本次结果未保存”。
- 超过 5 分钟的孤立预留可恢复并退还，不会退还已成功记录。

**实施**

- 试用分为 `POST /api/trial/start` 和 `POST /api/trial/consult`；试用咨询每次独立，
  不创建可恢复 session、turn、附件或历史。
- 匿名正文最多 3,000 字，注册正文最多 4,000 字，两者都由服务端再次校验。
- 配额桶使用北京时间日/月键；一次事务锁定所有适用桶，创建唯一预留并增加计数。
- 预留状态只允许 `reserved -> succeeded` 或 `reserved -> refunded`。
- DeepSeek 自动重试仍属于同一逻辑调用；未进入 DeepSeek 的本地路径不扣额度。
- 配额响应只公开个人剩余次数和重置时间，不公开 IP 或全站风控细节。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_trial_identity.py tests/test_trial_api.py tests/test_quota.py tests/test_network_identity.py tests/integration/test_quota_concurrency.py --basetemp='D:\agent development\pytest-temp-production-quota'
```

## 6. 有界 OCR/DeepSeek 执行与结果型 Provider 健康

**文件**

- 新增 `app/execution/__init__.py`
- 新增 `app/execution/bounded.py`
- 新增 `app/health/__init__.py`
- 新增 `app/health/service.py`
- 新增 `app/providers/health.py`
- 修改 `app/providers/deepseek.py`
- 修改 `app/attachments/service.py`
- 修改 `app/agent/pipeline.py`
- 修改 `app/api/health.py`
- 修改 `app/api/schemas.py`
- 修改 `app/deps.py`
- 修改 `app/main.py`
- 新增 `migrations/versions/20260810_0004_provider_health.py`
- 新增 `tests/test_bounded_execution.py`
- 新增 `tests/test_provider_health.py`
- 修改 `tests/test_attachments_service.py`
- 修改 `tests/test_pipeline.py`
- 修改 `tests/test_api.py`

**先写测试**

- OCR 同时执行 1 个、最多等待 2 个；DeepSeek 同时执行 2 个、最多等待 4 个。
- 超过等待容量立即返回繁忙；DeepSeek 等待超过 45 秒退出且配额退还。
- 取消、超时、重试和异常都释放执行槽；DeepSeek 的内部重试不重复占槽或扣额度。
- OCR 的 90 秒总预算覆盖等待和处理，所有失败路径删除原文件及未确认 sidecar。
- Provider 只有 Key、启动成功或健康端点访问时仍为 `unknown`，不会产生付费探测。
- 最近 30 分钟至少 4 个样本且失败率达到 50%，或连续 3 次最终失败，状态变为
  `degraded`；降级后连续 3 次成功才恢复 `healthy`。
- Provider 结果只记录逻辑调用 ID、模型、结果类别、耗时、token 和时间，不记录正文。
- 存活、就绪和 Provider 状态分离；数据库、法条库、临时目录或磁盘检查失败时就绪
  失败，但登录和删除能力仍有独立依赖。

**实施**

- `BoundedExecutor` 使用信号量、受锁等待计数和单调时钟，避免仅有信号量时等待者
  无上限。
- 配额预留成功后才进入 DeepSeek 队列；队列失败统一调用幂等退还。
- Provider 结果状态在 PostgreSQL 中持久化，由状态机更新，不从 API Key 推断。
- 提供 `/live`、`/ready` 和公开 Provider 状态；保留 `/health` 兼容投影，但移除
  “configured 等于健康”的旧语义。
- 磁盘达到保护阈值时拒绝新咨询和附件，继续允许认证、退出和数据删除。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_bounded_execution.py tests/test_provider_health.py tests/test_attachments_service.py tests/test_pipeline.py tests/test_api.py --basetemp='D:\agent development\pytest-temp-production-execution'
```

## 7. 公开 DeepSeek 锁定与内容隔离的管理诊断

**文件**

- 修改 `app/providers/catalog.py`
- 修改 `app/providers/factory.py`
- 修改 `app/api/providers.py`
- 修改 `app/api/consult.py`
- 修改 `app/api/schemas.py`
- 新增 `app/admin/__init__.py`
- 新增 `app/admin/models.py`
- 新增 `app/admin/service.py`
- 新增 `app/api/admin.py`
- 修改 `app/main.py`
- 修改 `app/deps.py`
- 新增 `tests/test_public_provider_policy.py`
- 新增 `tests/test_admin_service.py`
- 新增 `tests/test_admin_api.py`
- 修改 `tests/test_providers.py`
- 修改 `tests/test_api.py`

**先写测试**

- 生产公开请求体不再接受 `provider_id`，公开 Provider 列表最多只显示 DeepSeek。
- Fake Provider 不能通过请求参数、Cookie、Header 或公开目录被选择。
- 本地测试可显式注入 Fake，不会构造 DeepSeek HTTP 客户端。
- 普通用户和未登录用户不能访问管理接口。
- 管理接口只返回账号状态、验证状态、创建时间、配额、调用状态和脱敏错误类别，
  Schema 中不存在咨询正文、模型输出、附件确认文字或 Prompt 字段。
- 停用账号和吊销会话写入管理审计；管理员也不能通过管理接口读取咨询内容。

**实施**

- 公开 Pipeline 固定解析为 DeepSeek；删除前端选择能力和生产回退逻辑。
- `ProviderCatalog` 保留内部测试条目，但公开投影按部署策略过滤。
- 管理服务使用专用只读投影，不依赖或 join turn 内容表。
- 管理动作记录管理员 ID、动作、目标账号、时间和结果，不记录正文。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_public_provider_policy.py tests/test_admin_service.py tests/test_admin_api.py tests/test_providers.py tests/test_api.py --basetemp='D:\agent development\pytest-temp-production-provider-policy'
```

## 8. 前端试用、注册、登录、隐私、配额与历史工作流

**文件**

- 修改 `app/web/index.html`
- 修改 `app/web/styles.css`
- 修改 `app/web/js/api.js`
- 修改 `app/web/js/state.js`
- 修改 `app/web/js/app.js`
- 修改 `app/web/js/render.js`
- 新增 `app/web/js/auth.js`
- 新增 `app/web/js/captcha.js`
- 新增 `app/web/js/privacy.js`
- 修改 `app/web/icons/lucide.svg`
- 修改 `tests/test_api.py`
- 新增 `tests/test_web_security.py`
- 新增 `tests/e2e/test_public_beta_flows.py`

**先写检查**

- 未登录首屏直接提供 5 次纯文字试用；不显示附件、历史或 Provider 选择。
- 注册、邮箱验证、登录、退出、重发验证、忘记密码和重置密码均可完成。
- 验证与重置 token 只从 URL fragment 读取，用后立即从地址栏清除。
- 注册和首次咨询分别展示并记录当前隐私版本；版本变化后阻止发送直到重新确认。
- 登录后显示日/月剩余额度、重置时间、历史和附件；匿名只显示累计剩余试用次数。
- 第 6 次匿名试用和注册日/月额度耗尽时，按钮状态与 API 拒绝一致。
- 登出会清除页面内 CSRF、账号、历史和附件草稿状态，不清除独立匿名试用 Cookie。
- 页面不使用 `innerHTML`、`insertAdjacentHTML` 或 `localStorage` 保存敏感数据。
- 阿里云 CAPTCHA 只允许官方所需的精确 CSP 域名，不加入通配脚本或连接来源。
- 1366x768、936x900、390x844 和 360x800 下没有重叠、裁切、水平滚动或不可达控件。

**实施**

- 在现有工作台内增加紧凑账号区和认证对话框，不增加营销落地页。
- 前端状态明确区分 `trial`、`pending_verification`、`authenticated`、
  `disabled` 和 `capacity_full`。
- API 层统一附加 CSRF Header、保持同源 Cookie，并严格校验认证、隐私和配额响应。
- 试用与注册咨询调用不同端点；注册后不迁移匿名结果。
- 移除 Provider 分段选择，保留只读 DeepSeek 状态和繁忙反馈。
- CAPTCHA 加载失败时只禁用注册和首次试用，不伪装验证成功。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_api.py tests/test_web_security.py tests/e2e/test_public_beta_flows.py --basetemp='D:\agent development\pytest-temp-production-web'
node --check app/web/js/api.js
node --check app/web/js/state.js
node --check app/web/js/auth.js
node --check app/web/js/captcha.js
node --check app/web/js/privacy.js
node --check app/web/js/render.js
node --check app/web/js/app.js
```

## 9. 保留期清理、安全日志与删除恢复清单

**文件**

- 新增 `app/observability/__init__.py`
- 新增 `app/observability/logging.py`
- 新增 `app/observability/request_context.py`
- 新增 `app/deletion/__init__.py`
- 新增 `app/deletion/service.py`
- 新增 `app/integrations/oss.py`
- 新增 `app/jobs/__init__.py`
- 新增 `app/jobs/cleanup.py`
- 修改 `app/history/service.py`
- 修改 `app/api/sessions.py`
- 修改 `app/main.py`
- 新增 `migrations/versions/20260810_0005_deletion_retention.py`
- 新增 `deploy/logrotate/weiquan`
- 新增 `tests/test_safe_logging.py`
- 新增 `tests/test_cleanup_jobs.py`
- 新增 `tests/test_deletion_manifests.py`
- 新增 `tests/integration/test_retention.py`

**先写测试**

- 结构化日志过滤 Cookie、Authorization、密码、邮箱 token、验证码、正文、模型输出、
  文件名、OCR 文字、路径和秘密，只保留请求 ID、内部摘要、路由、状态、耗时和类别。
- 咨询及确认附件文字 30 天后级联删除；日志轮换最多 14 天。
- 临时附件和未确认 OCR 最长 1 小时；会话、令牌、IP 摘要和待验证账号分别按设计
  期限清理，单次清理有上限并可重复执行。
- 用户删除时资源立即从查询中消失；公开成功前，加密删除清单已上传私有 OSS。
- 删除清单只含咨询 ID 和删除时间，保留 35 天；加密或上传失败时不虚报成功。
- 删除中进程崩溃后，持久化 outbox 可继续上传并完成删除，不会恢复正文可见性。
- 清理与读取、续问、附件确认并发时不跨用户、不返回半条记录。

**实施**

- 请求中间件创建随机请求 ID，并对用户/试用身份做独立密钥 HMAC 摘要。
- 用户删除使用无内容 outbox：先把咨询标为不可见并记录删除意图，同步上传加密清单，
  再硬删除内容；后台任务恢复中断步骤。
- `app/jobs/cleanup.py` 提供有界批处理 CLI，由宿主机定时器调用，不在每个请求中执行
  无界清理。
- 应用 JSON 日志写入独立挂载目录，Nginx 与应用日志每日轮转、压缩并保留 14 天。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_safe_logging.py tests/test_cleanup_jobs.py tests/test_deletion_manifests.py tests/integration/test_retention.py --basetemp='D:\agent development\pytest-temp-production-retention'
```

## 10. 加密备份、恢复演练与监控

**文件**

- 新增 `deploy/backup/backup-postgres.sh`
- 新增 `deploy/backup/restore-postgres.sh`
- 新增 `deploy/backup/replay-deletions.sh`
- 新增 `deploy/backup/prune-backups.sh`
- 新增 `deploy/monitoring/check-services.sh`
- 新增 `deploy/systemd/weiquan-backup.service`
- 新增 `deploy/systemd/weiquan-backup.timer`
- 新增 `deploy/systemd/weiquan-cleanup.service`
- 新增 `deploy/systemd/weiquan-cleanup.timer`
- 新增 `docs/runbooks/backup-and-restore.md`
- 新增 `docs/runbooks/monitoring-and-alerts.md`
- 新增 `tests/test_backup_contract.py`
- 新增 `tests/test_monitoring_contract.py`

**先写测试**

- `pg_dump` 成功后才加密，校验后才上传；明文备份不会留在磁盘。
- 上传、加密或校验失败均返回非零并保留可诊断的无内容错误类别。
- 保留最近 7 份日备份和 4 份周备份，任何对象最大年龄不超过 28 天。
- 恢复流程验证解密、校验和、迁移版本和关键表可读，再重放删除清单及 30 天清理，
  最后才允许恢复公网写入。
- 每日备份满足最多丢失 24 小时数据的恢复点目标；隔离恢复演练从取得备份开始计时，
  在 4 小时内恢复基础服务，超出任一目标时演练失败并告警。
- 脚本不把数据库口令、加密材料、用户正文或命令展开写入日志。
- 监控覆盖 CPU、内存、swap、磁盘、容器重启、PostgreSQL、Nginx 5xx、队列、
  Provider 结果、邮件、验证码和临时附件年龄。

**实施**

- 使用 `pg_dump --format=custom` 和独立于 OSS 的非对称加密接收者；私钥不在 ECS
  仓库、日志或备份中。
- OSS Bucket 为私有，备份和删除清单使用不同前缀与生命周期规则。
- 每月恢复演练优先在隔离本地环境执行，并记录备份时间、校验、耗时和操作者。
- 告警阈值至少覆盖磁盘 80%、持续 swap、重复重启、备份失败和错误率突增。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_backup_contract.py tests/test_monitoring_contract.py --basetemp='D:\agent development\pytest-temp-production-ops'
```

## 11. Docker、宿主机 Nginx、部署与回滚

**文件**

- 新增 `Dockerfile`
- 新增 `.dockerignore`
- 新增 `deploy/compose.production.yml`
- 新增 `deploy/postgres/postgresql.conf`
- 新增 `deploy/nginx/weiquan.072988.xyz.conf`
- 新增 `deploy/nginx/audio.072988.xyz.conf`
- 新增 `deploy/scripts/preflight.sh`
- 新增 `deploy/scripts/deploy.sh`
- 新增 `deploy/scripts/rollback.sh`
- 新增 `deploy/scripts/smoke.sh`
- 新增 `docs/runbooks/deployment-and-rollback.md`
- 修改 `README.md`
- 新增 `tests/test_deployment_contract.py`

**先写测试**

- Compose 只有一个应用容器和一个 PostgreSQL 容器；PostgreSQL 没有宿主机端口。
- 应用只有一个 worker，绑定 `127.0.0.1:8001`，内存上限 1024 MiB；
  PostgreSQL 内存上限 512 MiB。
- PostgreSQL 参数为 `max_connections=20`、`shared_buffers=128MB`、
  `work_mem=4MB`、`maintenance_work_mem=64MB`。
- Nginx 把 `weiquan.072988.xyz` 代理到 `8001`，把 `audio.072988.xyz` 限定为
  MP3 只读静态目录；两个 server block 的日志和缓存规则互相独立。
- PostgreSQL、附件目录和内部管理端口均不可公网访问。
- 发布前迁移和备份失败时不切换镜像；健康检查失败可切回上一固定版本镜像。
- 自动冒烟只使用 Fake 外部集成或只读检查，不调用真实 DeepSeek。

**实施**

- 构建固定 Python 3.11 镜像，使用非 root 用户、只读应用代码和独立可写临时卷。
- Compose 引用服务器受控配置路径，不在仓库创建秘密文件。
- 宿主机 Nginx 终止 HTTPS；音频目录由小程序发布流程原子重命名，不进入应用容器。
- 部署顺序固定为预检、发布前加密备份、迁移、启动新镜像、存活/就绪检查、切换；
  回滚不触碰本地 `8000` 服务。

**验证**

```powershell
docker compose -f deploy/compose.production.yml config
.\.venv\Scripts\python.exe -m pytest -q tests/test_deployment_contract.py --basetemp='D:\agent development\pytest-temp-production-deploy'
```

## 12. 安全、并发、故障、浏览器与资源全量验收

**文件**

- 新增 `.github/workflows/ci.yml`
- 新增 `tests/security/test_auth_security.py`
- 新增 `tests/security/test_content_isolation.py`
- 新增 `tests/security/test_upload_security.py`
- 新增 `tests/integration/test_failure_matrix.py`
- 新增 `tests/integration/test_resource_limits.py`
- 修改相关现有测试和最小实现文件

**自动化验收**

- 运行现有全量测试，并验证法条引用和召回门禁不回归。
- PostgreSQL 并发测试覆盖第 101 个账号、配额不超发、附件双预留和跨用户删除。
- 故障矩阵覆盖 PostgreSQL、DirectMail、CAPTCHA、OSS、OCR、DeepSeek 429/5xx、
  非法响应、超时、取消、队列已满、磁盘只读和进程恢复。
- 安全测试覆盖 Argon2id、会话轮换、CSRF、同源、Cookie、Header 伪造、SQL 参数化、
  DOM 安全、上传签名、路径穿越、Prompt 注入和日志脱敏。
- CI 禁止真实 DeepSeek、DirectMail、CAPTCHA 和 OSS 网络请求。
- 资源测试按应用 1024 MiB、PostgreSQL 512 MiB 运行两个并发咨询和一个 OCR，
  验证无 OOM、无重复 worker、无跨用户数据。

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp='D:\agent development\pytest-temp-production-full'
.\.venv\Scripts\python.exe scripts/verify_refs.py
.\.venv\Scripts\python.exe scripts/check_recall.py
.\.venv\Scripts\python.exe -m pip_audit
node --check app/web/js/api.js
node --check app/web/js/state.js
node --check app/web/js/auth.js
node --check app/web/js/captcha.js
node --check app/web/js/privacy.js
node --check app/web/js/render.js
node --check app/web/js/app.js
git diff --check
```

**浏览器验收**

- 测试服务只监听 `127.0.0.1:8765`，不停止或复用 `8000`。
- Fake 外部集成完成匿名开始、5 次试用、第 6 次阻止、注册、验证、登录、隐私确认、
  配额、附件确认、续问、历史、删除、退出和密码重置。
- 用户 A 与用户 B 在两个独立浏览器上下文中交叉访问，全部安全失败。
- 断网、邮件失败、CAPTCHA 失败、队列繁忙、Provider 失败和历史未保存均有准确状态。
- 在四个目标视口检查长邮箱、长中文、3 个附件、验证码、认证对话框和错误状态，
  无重叠、裁切、布局跳动或横向滚动。
- 微信小程序 MP3 静态访问在咨询压力下保持可用。

## 13. 内部、10 人观察期与 100 账号分阶段开放

**文件**

- 修改 `app/config.py`
- 修改 `app/auth/service.py`
- 修改 `app/api/auth.py`
- 新增 `docs/runbooks/public-beta-rollout.md`
- 新增 `docs/runbooks/incident-response.md`
- 新增 `tests/test_rollout_gates.py`

**先写测试**

- `internal` 阶段关闭公开注册，只允许服务器创建的内部账号。
- `invited` 阶段仍关闭公开注册，由受控邀请命令创建最多 10 个观察账号。
- `public` 阶段开放注册，但数据库始终执行 100 个有效占位上限。
- 关闭新咨询时仍允许登录、退出、历史读取和用户删除。
- 未满足上线门槛时，配置不能切换到公网开放状态。

**开放顺序**

1. ICP 备案、DNS、两个 HTTPS 站点、DirectMail、CAPTCHA、私有 OSS、恢复演练、
   PostgreSQL 非公网和资源测试全部通过。
2. 内部账号验证登录、配额、附件、删除、监控和小程序音频互不影响。
3. 邀请 10 人连续观察 7 天；要求无跨用户事件、无配额超发、无 OOM、无备份失败，
   容器不反复重启，磁盘低于 80%，错误率和 swap 无持续恶化。
4. 用户再次明确批准后，只执行一次最小真实 DeepSeek 冒烟，核对 Provider、模型、
   request ID、usage、配额成功和日志脱敏。
5. 门槛持续满足后切换公开注册；达到 100 个有效占位后只关闭新注册。
6. 任一阶段出现所有权风险、配额超发、持续 OOM、数据库不稳定或恢复失败，立即停止
   扩量；必要时关闭新咨询并按 runbook 回滚。

**验证**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_rollout_gates.py --basetemp='D:\agent development\pytest-temp-production-rollout'
```

## 完成标准

- 匿名前 5 次真实逻辑调用成功，第 6 次及全站第 51 次稳定阻止，且匿名内容不落库。
- 注册账号拥有独立的北京时间日 10 次、月 50 次额度，失败退还且并发不超发。
- 第 101 个有效占位在并发条件下也无法创建。
- 所有公开资源按服务端 `user_id` 隔离，跨用户无法读取、继续、修改或删除。
- 原始附件和未确认 OCR 文字在全部结束路径及 1 小时兜底清理后不存在。
- 附件数量、10 MiB、20 页、12,000 字符以及 PDF/PNG/JPEG 三重类型校验边界均可
  自动验证。
- 公开路径固定 DeepSeek，Fake 不出现在请求、界面或公开目录。
- Provider 健康由真实结果计算，不因存在 Key 变为健康。
- 30 天内容、14 天日志、最长 28 天备份和 35 天删除清单保留期可验证。
- 每日加密备份、删除清单重放和每月隔离恢复演练可重复执行。
- 恢复演练证明恢复点不超过 24 小时，且基础服务恢复时间不超过 4 小时。
- 同一 ECS 上两个子域名稳定，两个咨询和一个 OCR 不触发 OOM。
- 全量离线测试、迁移、依赖审计、浏览器验收、故障矩阵和资源测试全部通过。
- 只有在全部公网门槛满足且用户明确批准真实冒烟后，才开放受控公测。
