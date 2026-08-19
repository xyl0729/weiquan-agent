# 贡献指南

感谢你愿意改进维权作战 Agent。这个项目同时涉及用户隐私、法律数据和模型输出边界，
因此除了代码质量，还必须保证来源可核验、行为可复现、失败时不编造。

## 开始之前

1. 阅读 [README](README.md)、[行为准则](CODE_OF_CONDUCT.md) 和
   [安全政策](SECURITY.md)。
2. 较大的功能或新场景请先开 Issue，说明用户问题、范围、数据来源和验收方式。
3. 安全漏洞不要开公开 Issue，请按安全政策私下报告。
4. 不要提交真实纠纷材料、个人信息、邮箱、Cookie、令牌、模型 Key 或生产配置。

## 本地开发

需要 Python 3.11。Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\ingest_statutes.py
.\.venv\Scripts\python.exe -m pytest
```

macOS 或 Linux 将 Python 路径替换为 `.venv/bin/python`。默认测试使用
`FakeProvider`，不得发起真实 DeepSeek 请求。

提交前至少运行：

```powershell
.\.venv\Scripts\python.exe scripts\verify_refs.py
.\.venv\Scripts\python.exe scripts\check_recall.py
.\.venv\Scripts\python.exe -m pytest
```

## 不同改动的要求

### 代码与界面

- 为行为变化补充聚焦测试，保持现有 API 和错误码兼容，或在 PR 中明确说明变更。
- 模型输出必须经过 schema、槽位、引用和所有权校验，不能直接取得确定性字段控制权。
- 前端不得把未确认 OCR 原文或敏感认证信息写入 `localStorage`、日志或 URL。

### 法条、规则与 Playbook

- 法条正文只接受权威来源，必须记录条号、生效日期和原文链接。
- 不接受由模型生成后未经人工逐字核验的法条数据。
- 新场景必须同时提供追问槽位、证据清单、确定性规则、法条引用和召回评测样本。
- 运行 `scripts/verify_refs.py` 与 `scripts/check_recall.py`，并说明人工核验结果。

### 文档与示例

- 使用虚构、最小化的数据；截图和日志不得包含真实账号或用户材料。
- 命令应能从仓库根目录运行，并明确 Windows 与类 Unix 差异。

## Pull Request 清单

- PR 聚焦一个问题，没有顺手重构无关模块。
- 新行为有测试，完整测试、引用门禁和召回门禁均通过。
- 没有加入秘密、个人信息、生成数据库或本地 `.env`。
- 用户可见变化已更新 README 或相关运行手册。
- 法律数据变化已列出权威来源和人工核验范围。

提交信息建议使用简洁的祈使句或现有的 Conventional Commit 风格，例如
`fix: preserve context on follow-up turns`。`Co-Authored-By` 只用于真实共同作者，不要把
自动化工具或模型标记为代码共同作者。
