# 校园传闻核验接入

本目录是外部校园传闻服务的接入层。源码仓库不带交付包、采集来源清单、浏览器状态、账号、原始帖子或监测数据库。未配置时 `start core` 会跳过该组件。

可将交付包放入 `server/CampusRumor/.runtime/delivery`，或设置 `CAMPUS_RUMOR_DIR` 指向包含 `app/main.py` 的目录。纯文字线索支持交付版 1.2 的旧存储接口。如果 Python 不在外部目录的 `.venv/Scripts/python.exe`，可指定 `CAMPUS_RUMOR_PYTHON`。通过主管理器运行 `start campus-rumor`；默认只监听 127.0.0.1:8100，可用 `CAMPUS_RUMOR_PORT` 更改。

需要在线来源时，在你部署的采集器中配置经授权的数据源。小红书采集默认关闭，启用需要 `CAMPUS_ENABLE_XHS=true`、`CAMPUS_XHS_QUERY` 和单独授权的浏览器。MainAgent 的凭据和官方来源列表用其本地 `.env.cloud` 配置。没有已核验的来源时，界面按待核消息展示。

## 可选：更新扫码授权流程

[v1.3.0 扫码授权补丁](patches/xhs-qr-v1.3.0.patch)适用于原版 `campus-agent-server-v1.3.0`。它让雷达页区分待扫码、待手机确认、二维码过期、超时和授权成功，二维码有变化时自动刷新。搜索遇到官方 RedNote 站点时也能识别结果，并把笔记链接统一成小红书地址。

把原版交付包解压到一个新目录。在本仓库根目录执行以下命令，将 `$package` 改为新解压的 `campus-agent` 路径：

```powershell
$package = 'D:\CampusRumor\campus-agent'
$patch = (Resolve-Path '.\server\CampusRumor\patches\xhs-qr-v1.3.0.patch').Path
git -C $package -c core.autocrlf=false apply --check --whitespace=error -- $patch
if ($LASTEXITCODE -eq 0) { git -C $package -c core.autocrlf=false apply --whitespace=error -- $patch }
```

检查通过后，补丁只更新 `xhs_client.py`、`app/core/xhs.py` 和 `app/web/static/radar.js`。需要在新部署中使用它时，把 `CAMPUS_RUMOR_DIR` 指向该目录，再按前面的接入步骤配置。
