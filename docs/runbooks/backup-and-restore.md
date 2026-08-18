# 加密备份与恢复

本文用于维权 Agent 的 PostgreSQL 日常备份和隔离恢复。生产目标是恢复点不超过
24 小时，基础服务恢复时间不超过 4 小时。未完成下列校验时，不得恢复公网写入。

## 密钥与存储边界

- OSS Bucket 必须为私有，`backups/` 与 `deletion-manifests/` 使用独立前缀和生命周期。
- 数据库口令、OSS 凭据和 `age` 接收者只由服务器受控环境提供，不写入仓库或命令参数。
- `age` 私钥不得放在 ECS、仓库、日志或 OSS 中；至少保存两份离线加密副本。
- ECS 只保存公开的 `age` 接收者。恢复时临时挂载私钥文件，权限设为 `0600`，结束后移除。
- `pg_dump` 明文只写 `/dev/shm`，退出时清理；磁盘和 OSS 中只保留加密文件及校验和。

## 每日备份

`weiquan-backup.timer` 每天北京时间 03:15 触发。应用容器的受控环境需提供：

`PGHOST`、`PGPORT`、`PGUSER`、`PGPASSWORD`、`PGDATABASE`、
`AGE_BACKUP_RECIPIENT`、`ALIYUN_ACCESS_KEY_ID`、
`ALIYUN_ACCESS_KEY_SECRET`、`OSS_ENDPOINT`、`OSS_BUCKET` 和
`BACKUP_STAGING_DIR`。

脚本依次执行 `pg_dump`、`age` 加密、SHA-256 校验、私有 OSS 上传和轮换。最近 7 份
日备份、4 份周备份保留，任何备份最长 28 天。成功后仅在
`BACKUP_STAGING_DIR/last-success.json` 写入时间和对象键，不包含正文或秘密。

检查最近一次任务：

```bash
sudo systemctl status weiquan-backup.service
sudo journalctl -u weiquan-backup.service --since today
sudo test -s /srv/weiquan/backup-staging/last-success.json
```

日志只应出现 `backup_succeeded`，或
`backup_failed category=<无内容类别>`。不得使用 `bash -x`。

## 每月隔离恢复演练

1. 记录操作者、开始时间、备份对象时间和演练编号，不记录用户内容。
2. 在与公网隔离的 PostgreSQL 实例创建空数据库，确认目标名称两次。
3. 从离线介质临时加载 `age` 私钥，设置 `ALLOW_ISOLATED_RESTORE=1`。
4. 配置 `RESTORE_PGHOST`、`RESTORE_PGPORT`、`RESTORE_PGUSER`、
   `RESTORE_PGPASSWORD`、`RESTORE_PGDATABASE` 和同值的
   `RESTORE_CONFIRM_DATABASE`。
5. 配置 OSS 只读凭据、`AGE_IDENTITY_FILE` 和独立的 `RESTORE_OUTPUT_DIR`。
6. 执行：

```bash
/usr/bin/bash deploy/backup/restore-postgres.sh \
  --object-key backups/daily/weiquan-YYYYMMDDTHHMMSSZ.dump.age
```

恢复脚本按顺序验证 SHA-256、`age` 解密、`pg_restore`、Alembic 版本和关键表，
随后重放 35 天内的删除清单并执行 30 天到期清理。仅当全部成功且总耗时不超过
4 小时，才生成 `restore-ready`。

## 演练判定

- 备份生成时间距演练开始不得超过 24 小时；仅演练历史备份时可显式设置
  `RESTORE_ALLOW_STALE=1`，并把它记为非 RPO 验收。
- `restore-ready` 不等于可以切公网。先用 Fake 外部集成验证 `/live`、`/ready`、
  登录、历史读取和删除，再人工核对删除清单重放数量。
- 任一校验失败、关键表不可读、删除清单失败或耗时超过 4 小时，演练失败并告警。
- 演练记录保留开始/结束时间、对象键、校验结果、迁移版本、耗时和操作者。
- 演练结束后销毁隔离数据库、明文临时目录和临时私钥挂载。

## 真实灾难恢复

先关闭新咨询和公网写入，再在隔离网络执行同一恢复流程。恢复完成后重复健康检查、
删除清单重放和 30 天清理，确认备份时间满足业务决策。只有负责人书面确认后才能
恢复公网写入；否则保持只读并继续排障或选择上一份已验证备份。
