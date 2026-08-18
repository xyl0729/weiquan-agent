# 维权作战 Agent

把日常纠纷整理为证据固定清单、时效提示和逐步行动方案。

**[在线体验 weiquan.072988.xyz](https://weiquan.072988.xyz)** · 无需注册即可试用

> 本项目提供信息整理与文书辅助，不构成法律意见，不预测个案结果。

押金被扣、预付卡跑路、退货被拒、加班费不给——这类纠纷里，多数人卡住的地方不是
"不知道自己有理"，而是不知道**先固定哪份证据、时效还剩多久、下一步该找谁**。

这个项目把这件事做成了确定性流水线：法条正文 100% 人工从全国人大法律法规数据库
录入，`verdict` 和时效只由本地规则产出，模型只负责读懂你的话和把已定结论说清楚。
**模型不能生成、也不能覆盖任何一条法律依据。**

## 它和"问大模型"有什么不同

| | 直接问通用大模型 | 本项目 |
|---|---|---|
| 法条来源 | 模型生成，可能虚构条号 | 人工录入的 90 条官方法条，`placeholder_data=false` |
| 结论产出 | 模型自由发挥 | 本地确定性规则独占 `verdict` |
| 时效计算 | 靠模型推理 | 辖区规则模块计算 |
| 数据不全时 | 照样给个像样的答案 | 停下来追问，或 `escalate`，不补写推测 |
| 可验证性 | 无 | `verify_refs.py` 门禁：每条 playbook 引用必须精确命中本地法条库 |

检索质量当前 `Recall@5 = 1.000 (92/92)`，覆盖 21 个评测分组。

## 当前状态

已上线公测：[weiquan.072988.xyz](https://weiquan.072988.xyz)，包含网页、登录/注册、
匿名试用、注册用户额度、材料上传核对、智能路由和真实 DeepSeek Provider。生产部署、
数据库迁移、加密 OSS 备份和隔离恢复验证均已完成。

额度规则：

- 未登录匿名试用：累计 5 次，仅支持文字咨询，不保存历史。
- 注册并登录：每日 10 次、每月 50 次，可保存历史并使用材料上传。
- 首次试用和注册会要求确认当前隐私政策；CAPTCHA 是否出现由服务端配置决定。

公开服务只允许真实 DeepSeek；`FakeProvider` 仅用于离线测试和本地演示，不能把
Fake 结果当作真实模型调用。

## 覆盖的 9 个场景

每个场景都有独立的 playbook（证据清单、追问槽位、确定性规则）和经 `verify_refs.py`
校验的法条引用：

- 租房押金扣减
- 预付卡与预付款纠纷
- 加班工资与劳动报酬
- 退货换货被拒
- 假货与商品欺诈
- 培训服务退费
- 自动续费与默认扣款
- 装修合同违约
- 小额诉讼程序

`app/playbooks/test_scenario.yaml` 仅用于开发测试，不属于正式场景。后续更新法条、
playbook 或评测样本时，必须重新执行人工核验清单和本地数据门禁。

项目不会抓取法条，也不会由模型生成法条正文。

## 工作方式

咨询请求按固定的 13 个阶段执行：

`请求校验 -> 会话恢复 -> playbook 注册表 -> 场景与事实抽取 -> 事实校验 -> 最多两轮追问 -> 本地检索 -> 确定性规则 -> 辖区/时效 -> 锁定草稿 -> 模板渲染 -> 持久化 -> 响应`

`FakeProvider` 只用于离线测试，公开服务只暴露并调用真实 DeepSeek。Provider 只负责
从用户消息中分类场景、抽取结构化事实和润色文字；场景和槽位会再次经过本地 schema
校验。法条、引用、日期、辖区规则、时效和 `verdict` 均由本地已核验数据与确定性
模块产生，模型不能生成或覆盖这些字段。

规则只允许有限的结构化节点，不使用 `eval()`、`exec()`、`simpleeval` 或自由工具
调用循环。依赖数据不完整时，流水线会停止或返回 `escalate`，不会补写推测性结论。

## 安装与离线运行

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\ingest_statutes.py
```

首次配置时，可以在 `.env` 不存在的情况下从模板创建：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

`.env.example` 是不含秘密的配置模板，不需要填写真实 Key。离线运行时确认根目录
`.env` 中为：

```ini
LLM_PROVIDER=fake
DEEPSEEK_API_KEY=
```

启动服务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

在另一个 PowerShell 窗口检查本地依赖：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health |
    ConvertTo-Json -Depth 5
```

离线模式下 `checks.provider` 应为 `offline`，且不会访问模型网络。

## 网页使用

本地服务启动后直接打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

匿名用户可以直接输入问题开始试用；首次提交时按页面提示确认隐私政策。注册账号后，
通过邮箱验证并登录，即可恢复咨询记录、上传最多 3 个 PDF/PNG/JPG/JPEG 材料。材料
会先提取文字，必须由用户核对并确认后才会发送给 Agent；上传材料不会把 OCR 原文
保存到浏览器的 `localStorage`。

生产网页的模型目录只显示 DeepSeek。每次响应都会显示实际 Provider、模型、request
ID 和脱敏用量；DeepSeek 失败时不会静默切换到 Fake。

## 咨询接口

注册用户的 `POST /api/consult` 接受 `session_id`、`message`、`jurisdiction` 和可选的
`attachment_ids`（最多 3 个）。下面的示例先创建会话，再使用返回的 `session_id` 补充
事实：

```powershell
$uri = "http://127.0.0.1:8000/api/consult"

$first = Invoke-RestMethod `
    -Uri $uri `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body (@{
        message = "房东不退押金"
    } | ConvertTo-Json)

$second = Invoke-RestMethod `
    -Uri $uri `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Body (@{
        session_id = $first.session_id
        message = "押金2000元，房东扣2000元，没说明理由，合同也没约定可以扣。"
        jurisdiction = "CN"
        attachment_ids = @()
    } | ConvertTo-Json)

$second | ConvertTo-Json -Depth 20
```

响应包含会话、turn 和审计 ID，以及状态、追问或方案、本地法条引用和脱敏后的模型
用量。业务状态为 `need_more_facts`、`ready` 或 `escalate`。

## DeepSeek 本地联调

真实联调会产生模型调用。只编辑项目根目录下未跟踪的 `.env`，不要把 Key 填入
`.env.example`：

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的实际Key
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

修改后完整停止并重新启动 Uvicorn，再调用 `/health`。`checks.provider` 为
`configured` 表示配置已加载；它不代表已经发起网络请求。随后可以直接运行上一节的
两轮 `/api/consult` 示例完成真实联调。

API 请求体或请求头不得携带模型 Key。显式选择 `deepseek` 但缺少 Key 时会返回脱敏的
配置错误，不会静默回退到 Fake。联调结束后，将 `.env` 中的 `LLM_PROVIDER` 改回
`fake` 并重启服务即可恢复纯离线模式。

## 数据门禁与测试

```powershell
.\.venv\Scripts\python.exe scripts\ingest_statutes.py
.\.venv\Scripts\python.exe scripts\verify_refs.py
.\.venv\Scripts\python.exe scripts\check_recall.py
.\.venv\Scripts\python.exe -m pytest
```

当前验收基线：

- 法条正文、条号、生效日期和来源链接已经人工核对。
- 92 条召回样本满足不少于 50 条的门槛。
- `Recall@5=1.000`，高于 `0.90` 门槛。
- 所有正式 playbook 引用都能精确命中本地法条库。
- 当前全量离线验收基线为 `841 passed, 12 skipped`，默认测试过程不发起 DeepSeek
  请求。12 个 skip 是 Postgres 集成测试，需配置 `TEST_POSTGRES_URL` 才会运行。

pytest 会为每次运行创建工作区内唯一的 `.tmp/pytest-<时间>-<进程>-<随机后缀>`
目录；`.tmp/` 已被 Git 忽略。这样即使上一次运行由提升权限创建了数据库，下一次
普通权限运行也不会尝试删除旧目录。需要指定临时目录时仍可显式传入
`--basetemp`。

## 安全边界

- 真实 DeepSeek Key 只从环境变量或未跟踪的根目录 `.env` 读取。
- `.env`、`data/app.db` 和 `data/statutes.db` 均被 Git 忽略。
- Key、Authorization Header 和完整模型 Prompt 不进入 API 响应、日志、数据库或审计记录。
- 用户消息在写入本地会话库前会清理看似 Key 或 Authorization Header 的内容。
- 模型输出必须通过严格 schema 和场景槽位白名单；本地规则独占 `verdict`。
- 不安装 FAISS、embedding 模型或 LangChain；邮箱认证、服务端会话与 CSRF 防护由
  项目自身实现。

## 生产部署

生产容器和宿主机 Nginx 配置位于 `deploy/compose.production.yml`。部署、冒烟与
回滚必须遵循 `docs/runbooks/deployment-and-rollback.md`；秘密配置只保存在服务器
受控文件 `/etc/weiquan/weiquan.env`，不得写入仓库。备案通过前保持应用只监听
`127.0.0.1:8001`，不要开放 `80/443`；备案通过后先用最新工作区构建新镜像，再
执行迁移、备份校验、健康检查和 Nginx/HTTPS 验收，最后再启用监控定时器。
