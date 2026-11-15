# 校园传闻核验接入

本目录是外部校园传闻服务的接入层。源码仓库不带交付包、采集来源清单、浏览器状态、账号、原始帖子或监测数据库。未配置时 `start core` 会跳过该组件。

设置 `CAMPUS_RUMOR_DIR` 指向自己部署的、包含 `app/main.py` 的目录。如果 Python 不在外部目录的 `.venv/Scripts/python.exe`，可指定 `CAMPUS_RUMOR_PYTHON`。通过主管理器运行 `start campus-rumor`；默认只监听 127.0.0.1:8100，可用 `CAMPUS_RUMOR_PORT` 更改。

需要在线来源时，在你部署的采集器中配置经授权的数据源。小红书采集默认关闭，启用需要 `CAMPUS_ENABLE_XHS=true`、`CAMPUS_XHS_QUERY` 和单独授权的浏览器。MainAgent 的凭据和官方来源列表用其本地 `.env.cloud` 配置。没有已核验的来源时，界面按待核消息展示。
