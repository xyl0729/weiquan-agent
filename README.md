# 维权作战 Agent

把日常纠纷整理为证据固定清单、时效提示和逐步行动方案。

> 本项目提供信息整理与文书辅助，不构成法律意见，不预测个案结果。

## 当前状态

项目按验证门禁分阶段实施。仓库当前只包含明显标注的测试法占位数据，
用于验证离线入库和检索管线。**上线或用于真实咨询前，必须由人工核对并替换
`data/seed_statutes.yaml`，同时补齐不少于 50 条人工标注的召回测试样本。**

项目不会抓取法条，也不会由模型生成法条正文。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\ingest_statutes.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/health` 检查服务状态。

## 数据门禁

```powershell
.\.venv\Scripts\python.exe scripts\verify_refs.py
.\.venv\Scripts\python.exe scripts\check_recall.py
.\.venv\Scripts\python.exe -m pytest
```

真实功能继续开发前必须满足：

- 法条正文、条号、生效日期和来源链接均经人工核对。
- 召回测试集至少 50 条。
- `Recall@5 >= 0.90`。
- 所有 playbook 引用都能精确命中本地法条库。

## 安全边界

- 真实 API Key 只通过环境变量或未跟踪的 `.env` 提供。
- 用户自带 Key 不入库、不写日志，仅随当前请求转发到后端。
- 规则表达式使用受限求值器，禁止 `eval()` 和 `exec()`。
- v1 不安装 FAISS、embedding 模型、LangChain 或登录系统。
