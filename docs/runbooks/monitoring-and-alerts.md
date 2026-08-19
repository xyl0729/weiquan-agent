# 监控与告警

`weiquan-monitor.timer` 每 5 分钟运行一次宿主机检查。脚本只读取聚合状态，不记录
邮箱、验证码、咨询正文、模型输出、附件名或路径。失败以无内容类别退出 1，由
systemd、阿里云云监控或外部通知规则接收。

## 覆盖范围

- CPU：读取负载并按 CPU 核数判断持续高负载。
- 内存与 swap：检查 `MemAvailable`；swap 持续高于阈值需要降载。
- 磁盘：使用率达到 80% 即告警并停止扩量。
- 容器重启：应用或 PostgreSQL 不在运行，或 `RestartCount` 异常增长时告警。
- 应用：检查 `/live`、`/ready` 和仅回环可达的 `/internal/metrics`。
- Nginx：检查服务状态及最近访问日志中的 5xx。
- 队列：OCR 和 DeepSeek queue 达到等待上限时告警。
- Provider：真实结果状态为 `degraded` 或不可读时告警，`unknown` 不做付费探测。
- DirectMail：最近 30 分钟失败达到阈值时告警。
- CAPTCHA：仅在 `CAPTCHA_ENABLED=true` 时处理最近 30 分钟服务失败告警；用户
  正常拒绝单独计数。
- 临时附件：目录不可读、扫描截断或最老文件超过 1 小时时告警。
- 备份：`last-success.json` 缺失、无效或超过 36 小时时告警。

## 默认阈值

| 信号 | 默认阈值 | 首次动作 |
| --- | --- | --- |
| 磁盘 | 80% | 关闭新咨询，检查日志、临时附件和备份轮换 |
| 可用内存 | 低于 10% | 检查容器内存和并发任务 |
| swap | 使用超过 20% 且连续三次 | 暂停扩量，排查 OOM 前兆 |
| 容器重启 | 累计超过 3 次或新增重启 | 查看无内容错误类别并准备回滚 |
| Nginx 5xx | 最近 1000 行达到 10 次 | 检查 `/ready`、PostgreSQL 和应用日志 |
| DirectMail | 30 分钟失败 3 次 | 关闭注册邮件入口，保留登录和删除 |
| CAPTCHA（启用时） | 30 分钟服务失败 5 次 | 关闭新试用和注册，不绕过验证 |
| 临时附件 | 最老文件超过 1 小时 | 手工运行有界 cleanup 并检查权限 |
| 备份 | 超过 36 小时无成功 | 立即执行受控备份并排查 OSS |

单次 CPU 或 swap 告警可观察；连续三次、指标持续恶化或同时出现 5xx 时按事故处理。

## 常用检查

```bash
sudo systemctl status weiquan-monitor.timer
sudo journalctl -u weiquan-monitor.service --since '-30 minutes'
curl --fail http://127.0.0.1:8001/live
curl --fail http://127.0.0.1:8001/ready
curl --fail http://127.0.0.1:8001/internal/metrics
docker inspect --format '{{.State.Status}} {{.RestartCount}}' \
  weiquan-app weiquan-postgres
```

`/internal/metrics` 必须由 Nginx 对公网返回 404，应用端口也只能绑定
`127.0.0.1:8001`。不得为了排障临时开放内部指标或 PostgreSQL 端口。

## 响应路径

1. 所有权、跨用户访问、配额超发或删除恢复风险：立即关闭新咨询，保持登录、历史
   读取、退出和用户删除，进入 incident runbook。
2. 磁盘、持续 swap、重复容器重启或 PostgreSQL 不稳定：停止扩量，创建发布前备份，
   必要时切回上一固定镜像。
3. 备份失败：不得发布数据库迁移或开放下一阶段；修复后补做备份及隔离恢复验证。
4. Provider 降级：暂停 DeepSeek 新调用并保留失败退还，不用 Fake Provider 替代。
5. DirectMail 或已启用的 CAPTCHA 故障：只关闭依赖该集成的新注册/试用路径，
   不影响既有账号；验证码关闭时不依赖 CAPTCHA 服务。
每次处置记录时间、请求 ID、聚合错误类别、版本和动作，不记录用户内容。
