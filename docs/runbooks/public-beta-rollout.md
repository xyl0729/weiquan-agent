# 公开测试分阶段开放手册

本手册用于在同一台 ECS 上逐步开放维权咨询站点，同时保持微信小程序音频站点独立可用。
生产应用只监听 `127.0.0.1:8001`，不得停止、复用或修改本机开发端口 `8000`。

## 配置开关

- `ROLLOUT_STAGE=internal|invited|public`：发布阶段，默认 `internal`。
- `INVITED_USER_LIMIT=10`：观察期有效账号上限，只能设置为 1 至 10。
- `PUBLIC_LAUNCH_APPROVED=false|true`：公开阶段的人工批准门禁。
- `NEW_WORK_ENABLED=false|true`：新咨询、匿名咨询和附件上传总开关。
- `REGISTRATION_CAPACITY_LIMIT=100`：数据库内有效账号占位总上限，不得超过 100。
- `CAPTCHA_ENABLED=false|true`：注册和首次匿名试用验证码开关，默认关闭，且不与
  发布阶段绑定。

`internal` 和 `invited` 都关闭网页公开注册。`invited` 账号只能通过服务器上的
`scripts/invite_user.py` 创建，且管理员、已停用账号和未过期待验证账号均占用观察期
名额。`public` 只在 production 配置同时设置 `PUBLIC_LAUNCH_APPROVED=true` 时通过
启动校验。当前内部、邀请和公开阶段均保持 `CAPTCHA_ENABLED=false`；以后只有用户
主动决定启用时才配置 CAPTCHA 场景。

## 公网前置门槛

进入任何公网观察前，逐项记录操作者、时间和结果：

1. `072988.xyz` 的网站 ICP 备案已通过，DNS 指向当前 ECS。
2. `weiquan.072988.xyz` 与 `audio.072988.xyz` 的 HTTPS、证书续期和独立 Nginx
   日志均验证通过。
3. PostgreSQL 无宿主机公网端口，应用只绑定 `127.0.0.1:8001`。
4. DirectMail 发信域和私有 OSS 权限验证通过；仅在
   `CAPTCHA_ENABLED=true` 时要求 CAPTCHA 场景验证通过。
5. 加密备份、隔离恢复、删除清单重放和 30 天清理演练通过；RPO 不超过 24 小时，
   基础服务 RTO 不超过 4 小时。
6. 全量测试、法条引用、召回、依赖审计、Docker 构建、迁移、故障矩阵、资源测试
   和浏览器验收全部通过。
7. 两个并发咨询加一个 OCR 时无 OOM、持续 swap、反复重启或音频访问异常。

任一项没有证据时，保持 `ROLLOUT_STAGE=internal`。

## 阶段一：内部验证

设置：

```text
ROLLOUT_STAGE=internal
PUBLIC_LAUNCH_APPROVED=false
NEW_WORK_ENABLED=true
CAPTCHA_ENABLED=false
```

先用 `scripts/create_admin.py` 创建管理员，再通过受控服务器命令创建内部账号。验证
登录、邮箱验证、日/月配额、附件确认、历史删除、账号停用、监控、备份和恢复。网页
注册返回 `public_registration_closed` 属于预期行为。

## 阶段二：10 人观察

设置：

```text
ROLLOUT_STAGE=invited
INVITED_USER_LIMIT=10
PUBLIC_LAUNCH_APPROVED=false
NEW_WORK_ENABLED=true
CAPTCHA_ENABLED=false
```

使用以下命令逐个邀请，命令不会绕过数据库全局 100 人上限：

```bash
python scripts/invite_user.py user@example.com --privacy-consent-recorded
```

连续观察至少 7 天。每天核对跨用户访问事件、配额超发、容器重启、OOM、swap、磁盘、
PostgreSQL、备份、邮件、OCR、DeepSeek 结果和两个子域名；验证码仅在启用时检查。
要求磁盘持续低于 80%，且无跨用户事件、配额超发、备份失败或持续资源恶化。

## 阶段三：最小真实冒烟

只有用户再次明确批准后才执行一次真实 DeepSeek 咨询。先保留观察期配置，核对实际
Provider、模型、request ID、usage、成功配额和脱敏日志。不得把 API Key、正文、
模型输出、附件文字、邮箱或 token 写入记录。冒烟失败时不重复消耗调用，按事件手册
处理并保持公开注册关闭。

## 阶段四：公开注册

只有前述门槛持续满足时才同时设置：

```text
ROLLOUT_STAGE=public
PUBLIC_LAUNCH_APPROVED=true
NEW_WORK_ENABLED=true
CAPTCHA_ENABLED=false
```

按 `deployment-and-rollback.md` 完成备份、迁移、固定镜像部署和只读冒烟。达到 100
个有效占位后，数据库会原子拒绝第 101 个账号；登录用户仍可继续使用，不得提高上限。

## 暂停和退回

发现所有权风险、配额超发、持续 OOM、数据库不稳定、恢复失败或磁盘风险时，先设置
`NEW_WORK_ENABLED=false` 并重新部署。该开关只停止新咨询、匿名咨询和附件上传；
登录、退出、历史读取、咨询删除、附件读取/确认/删除仍保持可用。

需要停止扩量时，将 `ROLLOUT_STAGE` 退回 `invited` 或 `internal`，并保持
`PUBLIC_LAUNCH_APPROVED=false`。镜像或迁移故障按 `deployment-and-rollback.md`
回滚；数据恢复按 `backup-and-restore.md` 执行。任何回退都不得影响
`audio.072988.xyz` 的静态 MP3 目录和发布流程。
