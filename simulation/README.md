# 校务模拟后端

五个后端共享 HTTP 工具约定：`GET /healthz`、`GET /agent/tools`、`POST /agent/call`。工具参数为 JSON；不同语言通过 MINTA 的标准 HTTP/JSON 接入。下表命令在各模块目录运行。

| 模块 | 准备方式 | 初始状态 |
| --- | --- | --- |
| Gymnasium / Python | 创建 `.venv` 并安装 `requirements.txt` | 无场馆、无预约 |
| Club / Node.js | `npm ci` | 无社团、无活动；通过已有管理接口登记及发布 |
| Library / Java | `.\compile.ps1` | 无馆藏、无借阅 |
| Classroom / .NET | `dotnet build Classroom.csproj` | 无教室、无预约 |
| Schedule / Rust | `cargo build --locked` | 无课表，保留时段规则 |

MINTA 网关单独维护在 [MlNTA](https://github.com/FIRENESST/MlNTA)。从项目根目录运行下面的命令获得本快照使用的实现；`MlNTA/` 被 Git 忽略。

```powershell
git clone https://github.com/FIRENESST/MlNTA.git MlNTA
git -C MlNTA checkout --detach a8bf90265c3ca326dd7a5082d2d2e5ff8dd77b19
cargo build --manifest-path MlNTA/core/Cargo.toml --locked -p minta-gateway
```

Windows 主管理器的 `start mintagw` 启动五个后端后按 [mintagw.toml](mintagw.toml) 启动网关。已有二进制可通过 `MINTAGW_EXE` 指定。模拟网关和后端默认仅监听本机；不要把这些测试服务的管理接口直接用作面向普通用户的入口。

## 添加自己的资料

在 `local-data/` 或仓库外创建 UTF-8 JSON 文件，再在启动环境中设置绝对路径。下表是字段约定，默认无需创建文件。

| 变量 | 格式 |
| --- | --- |
| `GYM_VENUES_FILE` | 数组，条目包含 `id`、`name`、`capacity`、`courts`、`court_type`、`places` |
| `CLASSROOM_ROOMS_FILE` | 数组，条目包含 `roomId`、`name`、`capacity`；ID 不重复 |
| `LIBRARY_CATALOG_FILE` | 数组，条目包含 `id`、`title`、`author`、`category`、`copies`；分类为 `literature/history/science/computing/social-science/philosophy/arts/media` |
| `SCHEDULE_DATA_FILE` | 对象，键是部署自己的学号，值是课程数组；课程含 `course_id`、`course_name`、`teacher`、`staff_number`、`classroom`、`day`、`start_period`、`end_period`、`weeks` 和可选 `points` |

课表访问由配置的校园 SSO 或审核后的资料确定对应学号/工号。日期计算还需在 MainAgent 设置 `CLOUD_TERM_ID`、`CLOUD_TERM_NAME`、`CLOUD_TERM_START`（第一周周一，格式为 YYYY-MM-DD）；未配置时不生成课程日历事件。

本目录用于模拟。产生的借阅、预约和活动状态保存在各服务 `.data/`，不会提交到仓库。
