# 生产部署与回滚

本文适用于 `weiquan.072988.xyz` 的单机生产部署。应用只映射
`127.0.0.1:8001`，宿主机 Nginx 负责 HTTPS；PostgreSQL 不发布宿主机端口。
现有本地开发服务使用的 `8000` 端口不属于本流程，任何部署或回滚命令都不得
停止、占用或复用它。

## 上线前置条件

在服务器发布前，必须确认：

- 网站 ICP 备案已经通过，两个子域名 DNS 已指向服务器。
- `weiquan.072988.xyz` 和 `audio.072988.xyz` 均已取得有效 HTTPS 证书。
- `/etc/weiquan/weiquan.env` 已由服务器管理员创建，权限为 `0600`，不进入仓库。
- DirectMail、私有 OSS、DeepSeek 和 `age` 备份接收者配置已完成。
- 当前发布显式设置 `CAPTCHA_ENABLED=false`。只有以后主动改为 `true` 时，才需要
  配置并验证 CAPTCHA 场景 ID 和前缀。
- `data/seed_statutes.yaml` 和召回基准已随代码检出；CI 与镜像构建中的法条引用、
  召回门禁均已通过。生成的 `data/statutes.db` 只存在于镜像内且保持只读。
- `/srv/audio` 由小程序音频发布流程管理；维权应用容器不得写入该目录。
- 已完成隔离恢复演练。真实 DeepSeek 冒烟仍需用户再次明确批准。

`/etc/weiquan/weiquan.env` 至少提供生产 `Settings` 所要求的字段、数据库初始化
字段 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`，以及备份脚本所需的
`PGHOST=postgres`、`PGPORT=5432`、`PGDATABASE`、`PGUSER`、`PGPASSWORD` 和
`AGE_BACKUP_RECIPIENT`。不要在终端输出、提交或复制该文件内容。
验证码保持关闭时，不要求 `CAPTCHA_SCENE_ID` 或 `CAPTCHA_PREFIX`。

## 首次准备

安装 Docker Engine、Compose 插件、Nginx 和 Certbot。然后由管理员创建目录：

```bash
sudo install -d -o 10001 -g 10001 -m 0700 \
  /srv/weiquan/attachments \
  /srv/weiquan/backup-staging \
  /srv/weiquan/logs
sudo install -d -o root -g root -m 0750 \
  /srv/weiquan/release-state
sudo install -d -o root -g root -m 0755 /srv/audio
```

systemd 单元使用稳定路径 `/srv/weiquan/current`。从待发布代码的仓库根目录创建
或更新该链接，并安装宿主机配置：

```bash
sudo ln -sfn "$(pwd)" /srv/weiquan/current
sudo install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer \
  /etc/systemd/system/
sudo install -m 0644 deploy/logrotate/weiquan /etc/logrotate.d/weiquan
sudo systemctl daemon-reload
sudo logrotate --debug /etc/logrotate.d/weiquan
```

两个 Nginx 配置直接引用正式证书路径，因此首次签发证书前不要启用它们。先用
Certbot 的临时 HTTP/ACME 配置完成证书签发，再安装并启用正式配置：

```bash
sudo install -m 0644 deploy/nginx/weiquan.072988.xyz.conf \
  deploy/nginx/audio.072988.xyz.conf /etc/nginx/sites-available/
sudo ln -sfn /etc/nginx/sites-available/weiquan.072988.xyz.conf \
  /etc/nginx/sites-enabled/weiquan.072988.xyz.conf
sudo ln -sfn /etc/nginx/sites-available/audio.072988.xyz.conf \
  /etc/nginx/sites-enabled/audio.072988.xyz.conf
sudo nginx -t
sudo systemctl reload nginx
```

公网请求 `/internal/` 必须返回 404；运维指标只允许从服务器回环地址读取。第一次
成功发布后先手工执行并确认一份加密备份，再启用定时任务，避免尚无备份时产生误报：

```bash
sudo systemctl start weiquan-backup.service
sudo systemctl status weiquan-backup.service
sudo systemctl enable --now \
  weiquan-monitor.timer weiquan-backup.timer weiquan-cleanup.timer
```

## 预检与发布

`IMAGE_REF` 必须是固定版本标签或镜像摘要，禁止使用 `latest`。`IMAGE_SOURCE`
只允许设为 `pull` 或 `build`，默认值是 `pull`：

- `pull` 拉取已经发布到镜像仓库的固定镜像。
- `build` 从服务器当前检出的代码构建并赋予 `IMAGE_REF` 标签，不需要购买私有
  镜像仓库，但会占用服务器的构建时间、内存和网络。

使用镜像仓库时，从仓库根目录执行：

```bash
export IMAGE_SOURCE='pull'
export IMAGE_REF='registry.example/weiquan-agent:2026.08.10-1'
sudo --preserve-env=IMAGE_REF,IMAGE_SOURCE \
  /usr/bin/bash deploy/scripts/preflight.sh
sudo --preserve-env=IMAGE_REF,IMAGE_SOURCE \
  /usr/bin/bash deploy/scripts/deploy.sh
```

不使用镜像仓库时，改用本地固定标签：

```bash
export IMAGE_SOURCE='build'
export IMAGE_REF='weiquan-agent:2026.08.11-1'
sudo --preserve-env=IMAGE_REF,IMAGE_SOURCE \
  /usr/bin/bash deploy/scripts/preflight.sh
sudo --preserve-env=IMAGE_REF,IMAGE_SOURCE \
  /usr/bin/bash deploy/scripts/deploy.sh
```

首次空数据库部署还需要显式确认：

```bash
export ALLOW_INITIAL_DEPLOY=1
sudo --preserve-env=IMAGE_REF,IMAGE_SOURCE,ALLOW_INITIAL_DEPLOY \
  /usr/bin/bash deploy/scripts/deploy.sh
unset ALLOW_INITIAL_DEPLOY
```

`preflight.sh` 检查 Docker Compose、受控环境文件、法条种子、可写私有目录、
Nginx 配置和 `127.0.0.1:8001` 边界。`deploy.sh` 的固定顺序是：

1. 运行预检，并按 `IMAGE_SOURCE` 拉取或构建固定镜像。
2. 在镜像内运行法条引用和召回门禁，确认只读法条库已经生成。
3. 以 `--no-recreate` 启动或复用 PostgreSQL，禁止应用发布替换已有数据库容器，
   并在 120 秒内等待真实查询成功；超时会停止发布。
4. 已有数据库先运行加密的 `backup-postgres.sh`；任何失败都会停止发布。
5. 运行 `alembic upgrade head`。
6. 只更新应用容器，不重建 PostgreSQL。
7. 运行只读 `smoke.sh`，检查 `/live`、`/ready` 和 `/internal/metrics`。
8. 冒烟成功后才写入 `current-image` 和 `previous-image` 状态。

PostgreSQL 配置变更不属于普通应用发布。需要变更数据库容器配置时，必须另开维护
窗口，在完成备份并明确批准停机后执行，不能移除上述 `--no-recreate` 保护。

自动冒烟不调用 `/api/consult`，也不会调用真实 DeepSeek、DirectMail、CAPTCHA
或 OSS 业务接口。发布后运行：

```bash
docker compose -f deploy/compose.production.yml ps
sudo /usr/bin/bash deploy/scripts/smoke.sh
sudo /usr/bin/bash deploy/monitoring/check-services.sh
```

## 应用回滚

健康检查失败时，使用：

```bash
sudo /usr/bin/bash deploy/scripts/rollback.sh
```

`rollback.sh` 只把应用切回 `previous-image` 并重新执行 `smoke.sh`。它不会执行
`alembic downgrade`，也不会自动运行 `restore-postgres.sh`。因此每次迁移必须保持
向前兼容上一应用版本。数据库损坏或迁移不可向前修复时，先关闭新咨询，再严格按
`backup-and-restore.md` 在隔离环境验证备份；恢复数据库需要独立人工批准。

## 故障停止条件

以下任一情况都必须停止发布或扩量：备份失败、迁移失败、`/ready` 非 200、
PostgreSQL 重启、持续 swap/OOM、磁盘达到 80%、出现跨用户访问、配额超发、
音频子域不可用或 Nginx 5xx 持续升高。保留无内容的时间、镜像版本、请求 ID 和
错误类别，禁止记录密码、令牌、用户正文、OCR 文本或文件名。
