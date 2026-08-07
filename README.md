# 维权作战 Agent

把日常纠纷整理为证据固定清单、时效提示和逐步行动方案。

> 本项目提供信息整理与文书辅助，不构成法律意见，不预测个案结果。

## 当前状态

实施计划的 9 个阶段已经完成。仓库当前包含从全国人大法律法规数据库逐部读取、并由
用户依据核验清单逐条确认的 61 条官方法条，以及覆盖 8 个召回评测分组的 64 条人工
核验样本。两个数据文件的 `placeholder_data` 均为 `false`；当前
`Recall@5=1.000 (64/64)`。

现有 9 个正式咨询场景：

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

`FakeProvider` 和 `DeepSeekProvider` 只负责从用户消息中分类场景、抽取结构化事实。
场景和槽位会再次经过本地 schema 校验。法条、引用、日期、辖区规则、时效和
`verdict` 均由本地已核验数据与确定性模块产生，模型不能生成或覆盖这些字段。

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

## 咨询接口

`POST /api/consult` 只接受 `session_id`、`message` 和 `jurisdiction`。下面的示例先创建
会话，再使用返回的 `session_id` 补充事实：

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
- 64 条召回样本满足不少于 50 条的门槛。
- `Recall@5=1.000`，高于 `0.90` 门槛。
- 所有正式 playbook 引用都能精确命中本地法条库。
- 默认离线测试为 330 项，测试过程不发起 DeepSeek 请求。

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
- v1 不安装 FAISS、embedding 模型、LangChain 或登录系统。
