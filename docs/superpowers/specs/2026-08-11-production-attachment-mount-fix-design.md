# 生产附件目录挂载修复设计

## 背景

生产 Compose 将宿主机私有附件目录
`/srv/weiquan/attachments` 原样挂载到容器内同一路径，并将
`ATTACHMENT_TEMP_DIR` 设置为该路径。附件服务为防止上传内容进入静态目录或
不受控位置，要求其容器内临时目录位于项目根目录 `/app` 下的非静态私有目录。
因此生产配置可以通过 `Settings` 校验，但应用启动时会被附件服务拒绝。

本次首次部署已完成空数据库迁移，但应用从未通过健康检查，也没有接收用户请求。

## 方案

保留宿主机目录、所有权和权限不变，仅调整容器内路径：

- 宿主机源目录：`/srv/weiquan/attachments`
- 容器目标目录：`/app/.runtime/attachments`
- `ATTACHMENT_TEMP_DIR`：`/app/.runtime/attachments`

`/app/.runtime/attachments` 位于项目根目录内，但不在静态资源目录
`/app/app/web` 内。该路径由只读根文件系统上的独立可写 bind mount 提供，
容器仍以 UID/GID `10001:10001` 运行，且继续移除全部 Linux capabilities。

不修改附件业务校验，不移动宿主机数据，不改变公网端口、Nginx、OSS、DeepSeek、
DirectMail 或另一个小程序的任何配置。

## 验证

部署契约测试必须确认：

- Compose 中 `ATTACHMENT_TEMP_DIR` 等于 `/app/.runtime/attachments`；
- 附件 bind mount 的源目录仍是 `/srv/weiquan/attachments`；
- bind mount 目标等于 `/app/.runtime/attachments`；
- 目标位于 `/app` 下且不位于 `/app/app/web` 下；
- 应用仍为只读根文件系统、非 root 用户和回环地址 `127.0.0.1:8001`。

修复后运行部署契约测试、附件相关测试和全量测试，再生成新的固定版本发布包。
服务器重新部署后，必须通过 `/live`、`/ready` 和 `/internal/metrics` 的只读
冒烟检查。备案通过前不启用 Nginx、证书或公网 `80/443`。

## 失败数据库处理

失败部署创建的 PostgreSQL 卷仅包含初始化迁移，没有用户数据。重新部署前先
只读确认应用容器未曾健康运行且业务表记录数为零。删除该卷属于破坏性操作，
必须获得用户单独明确批准；在批准前不删除卷，也不触发真实 OSS 备份。
