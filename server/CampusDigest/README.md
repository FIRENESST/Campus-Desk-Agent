# 校园日报接入

`server/CampusDigest` 是独立日报服务的接入层（索引、HTTP 查询、公众号正文与轮询管理）。新仓库没有日报运行包、采集数据、来源名录、登录会话或历史文件。

1. 部署自己的日报运行包，在主管理器启动环境中设置 `CAMPUS_DIGEST_DIR`，指向包含 `server/` 和 `runtime/python/python.exe` 的目录。未安装时 `start core` 会跳过该组件。
2. 在外部运行包配置已授权的公开来源。微信公众号入口如需登录，在自己的管理台完成授权。凭据保存在独立运行包的本地私有状态。
3. 主管理器中运行 `start campus-digest`，默认本机端口 8184。可统一设置 `CAMPUS_DIGEST_PORT`。查询索引 `DataBase/CampusDigest/digest.sqlite3` 由服务运行时创建。

本机查询包含 `GET /healthz`、`GET /api/campus-desk/search`、`/api/campus-desk/revision`、`/api/campus-desk/wechat`、`/api/campus-desk/wechat/accounts` 和 `/api/campus-desk/wechat/events`。公众号正文、关注与采集管理走内部鉴权接口。普通用户经统一接口进入，不直接连接采集服务。
