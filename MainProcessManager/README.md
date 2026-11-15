# MainProcessManager 本机主管理器

主管理器是独立的 Windows 进程。它负责项目进程登记、启动和停止，也是开发超级账户的登录入口。受管统一接口管理器通过 Windows 本机命名管道，请主管理器校验每次请求的调用进程身份。

## 初始化与登录

在项目根目录的交互终端运行。主管理器只使用 Python 标准库，可复用现有 Python 3.14 环境。

```powershell
py -3.14 -B -m MainProcessManager init
py -3.14 -B -m MainProcessManager run
```

也可以用 `.\server\UnifiedInterfaceManagement\.venv\Scripts\python.exe` 替换 `py -3.14`。首次初始化时自选账号和 15–128 字符的密码。密码使用隐藏输入，本地保存随机盐和 scrypt 哈希。以后用 `run` 登录。单个状态目录同一时间只允许一个主管理器会话。

默认状态目录在当前用户的 `%LOCALAPPDATA%\MainProcessManager\<工作区标识>`。`--state-dir` 可指定由当前 Windows 用户保护的本机私有目录。超级账户的权限覆盖项目本地控制面。账号、进程登记和操作记录保存在 `control.sqlite3`。

## 进程管理

| 控制台命令 | 作用 |
| --- | --- |
| `status [名称]` | 查看登记、运行状态和受管 PID |
| `start core` | 启动 PostgreSQL、llmServer、RAG 查询、MCP、教师凭据、CAS、校园日报、校园谣言防控、MainAgent、统一接口管理器和安卓下载服务；未配置的日报和传闻服务会跳过 |
| `start user-website` | 启动普通用户网站及其 MainAgent、统一接口管理器、CAS 和安卓下载服务依赖 |
| `start <名称>`、`start all` | 启动指定进程及其依赖，或启动所有登记项 |
| `stop <名称>`、`stop all` | 停止本窗口启动的进程。全停按启动顺序倒序执行 |
| `restart <名称>` | 重启指定进程。停止前先处理运行中的依赖方 |
| `reload` | 热重启已运行的应用进程，重试本次登录期间上次未恢复的进程 |
| `external help` | 查看校园助手与 CAS 的局域网和 FRP 入口管理命令 |
| `credentials issue\|update\|verify\|revoke` | 签发、修改、核验和撤销教师凭据 |
| `cas help` | 查看本地 CAS 账号的创建、学工号分配、密码重置和启停命令 |
| `admin help` | 查看按用户 UUID 授予、撤销管理权限及人工认证邮箱的命令 |
| `register <名称> <JSON文件>` | 保存新增进程定义 |
| `unregister <名称>` | 删除已停止且无其他登记项依赖的自定义进程 |
| `logs <名称>` | 查看对应的日志文件路径 |
| `audit`、`passwd`、`lock` | 查看最近操作、修改密码、锁定控制台 |
| `exit` | 停止本会话的受管进程并退出 |

核心进程名为 `postgresql`、`llmserver`、`rag-query`、`mcpserver`、`teacher-credentials`、`cas-server`、`campus-digest`、`campus-rumor`、`main-agent` 和 `unified-interface`。独立 Worker `main-worker`、桌面程序 `rag-writer`、`sim-gymnasium`、`sim-club`、`sim-library`、`sim-classroom`、`sim-schedule` 和 `mintagw` 也已登记。`start mintagw` 会启动五个模拟后端。启用独立 Worker 时，MainAgent API 可在自己的配置中设 `CLOUD_WORKERS=0`，主管理器会给 `main-worker` 配置一个执行槽。

修改应用代码后，可在控制台直接输入 `reload`。范围包括已运行的 `mcpserver`、`teacher-credentials`、`cas-server`、`campus-digest`、`campus-rumor`、`main-agent`、`main-worker`、`unified-interface`、`user-website` 及依赖它们的已运行受管服务。主管理器先停止依赖方，再先启动上游。每个进程按原有就绪条件检查。停机阶段出错时会尝试恢复已停止的服务。启动失败或等待依赖的进程会在同一主管理器进程的下次 `reload` 中重试。手动 `stop` 会取消该服务及依赖方的待重试状态。切换期间请求可能中断。网站可恢复已勾选「记住我」且仍有效的邮箱登录。

受管进程沿用各自的运行环境、数据和配置。启动 MainAgent 前配置 `server/MainAgent/.env.cloud` 或 `CLOUD_DATABASE_URL`，并准备已迁移的 PostgreSQL。需要桌面资料录入时单独启动 `rag-writer`，它会等待当前操作完成再关闭。状态为 `external` 的 PostgreSQL 可以供项目依赖使用，停止时由原入口管理。

校园日报也可单独执行 `start campus-digest`，通过 `logs campus-digest` 查看主管理器日志路径。首次使用新登记需要退出并重新进入主管理器。服务默认监听本机 `127.0.0.1:8184`。 [校园日报接入](../server/CampusDigest/README.md)

校园谣言防控服务通过 `start campus-rumor` 单独启动，使用 `status campus-rumor` 查看状态，使用 `logs campus-rumor` 查看日志路径。在线 `/admin` 的「运行总览」也能通过主管理器启停及重启。服务默认监听本机 `127.0.0.1:8100`。 [谣言防控服务接入](../server/CampusRumor/README.md)

普通用户网站默认监听本机 8082。 [网站启动与配置](../server/UserWebsite/README.md)

安卓下载服务登记为 `android-app-download`，监听 `127.0.0.1:8186`。普通网站、局域网和 FRP 的现有网站入口都会自动启动这个依赖，并代理 APK 下载和扫码入口。首次使用新登记时重新进入主管理器。 [安卓版构建和维护](../client/AndroidCalendar/README.md)

本地 CAS 账号可以在 CAS 自己的管理台创建，也可用 `cas add <学工号>` 创建。首次使用 CAS 管理台前，执行 `cas admin passwd` 设置独立 CAS 管理员的密码。CAS 使用独立的 Python 环境和主管理器私有状态目录下的 SQLite 数据库。 [CAS 启动、管理与绑定](../server/CASserver/README.md)

首次启用校务办理时，先完成 PostgreSQL `006_minta_operations.sql` 迁移，在 `simulation/Classroom` 构建教室服务，再重新进入新版主管理器并启动 `core`、`mintagw` 和 `user-website`。需要单独启动教师凭据服务时使用 `start teacher-credentials`。 [数据库迁移](../server/MainAgent/main_agent/cloud/README.md#安装与迁移) · [教师凭据管理](../DataBase/TeacherCredentials/README.md)

启用聊天重试和校务撤回时，先将 PostgreSQL 迁移到 `008_chat_effect_tracking_default.sql`。更新后重新启动已运行的 `sim-club`，再通过 `reload` 更新 MainAgent、Worker、教师凭据、统一接口和网站。 [聊天撤回](../server/MainAgent/main_agent/cloud/README.md#重来与撤回)

更新图书借期时，先执行 PostgreSQL 迁移 `012_library_loan_periods.sql` 并运行 `.\simulation\Library\compile.ps1`。已启动 `mintagw` 时，先执行 `stop mintagw`，再执行 `restart sim-library` 和 `start mintagw`，最后执行 `reload`。新借阅记录保存在 `simulation/Library/.data/loans.json`。旧版模拟后端只在内存中保存借阅，首次切换会清空该进程中尚未归还的旧借阅。

馆藏目录不随源码发布。用 LIBRARY_CATALOG_FILE 配置本地目录，见 [模拟服务说明](../simulation/README.md)。

启用场馆分时预约需要迁移 `014_gym_slots.sql`。场馆新记录保存在 `simulation/Gymnasium/.data/reservations.sqlite3`，旧版模拟服务在内存中保存的预约会随进程重启清空。课程示例用 `cargo build --locked --offline --manifest-path .\simulation\Schedule\Cargo.toml` 编译。若模拟服务已运行，在主管理器依次执行 `stop mintagw`、`stop sim-schedule`、`restart sim-gymnasium`、`restart sim-club`，完成编译后执行 `start mintagw` 和 `reload`。

更新校园活动的起止时间功能时，执行 `stop mintagw`、`restart sim-club`、`start mintagw` 和 `reload`。社团服务启动时自动更新其 SQLite 数据库，已有报名继续关联原活动。

启用学期报告时，在主管理器执行 `stop mintagw` 和 `stop sim-schedule`，再在项目根目录运行 `cargo build --locked --offline --manifest-path .\simulation\Schedule\Cargo.toml`。编译完成后执行 `restart sim-club`、`start mintagw` 和 `reload`。社团服务启动时自动创建报名查询索引，PostgreSQL 无需迁移。 [学期报告](../server/UserWebsite/README.md#校园组件窗口)

### 个人日程与教室分时预约

用数据库所有者连接执行迁移 `017_personal_schedule.sql`。如教室服务正在运行，在主管理器先执行 `stop mintagw` 和 `stop sim-classroom`，再在项目根目录执行 `dotnet build .\simulation\Classroom\Classroom.csproj`。编译完成后，按原有启动方式启动 `mintagw` 和 Agent，已运行的 Agent 和网站可用 `reload` 更新。新版教室服务将预约保存到 `simulation/Classroom/.data/reservations.json`，重启会保留。旧版存在内存中的预约在旧进程退出时清空，PostgreSQL 里的旧回执缺少时段。 [数据库迁移](../server/MainAgent/main_agent/cloud/README.md#安装与迁移)

### 模拟服务网页更新

更新模拟服务的独立网页时，先用 `stop mintagw` 停止依赖它们的网关，再停止正在运行的 `sim-classroom` 和 `sim-schedule` 以释放可执行文件。随后在项目根目录构建。

```powershell
dotnet build .\simulation\Classroom\Classroom.csproj
cargo build --locked --offline --manifest-path .\simulation\Schedule\Cargo.toml
.\simulation\Library\compile.ps1
```

构建完成后，重启已运行的 `sim-gymnasium`、`sim-club`、`sim-library`，再用 `start mintagw` 启动网关和所需依赖。

每个服务由单独的 Windows Job Object 持有。主管理器按实际进程归属判断服务状态，控制过程读取本地 Windows 状态。受管 PostgreSQL 通过 `pg_ctl` 安全关闭。 [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

新增服务的 JSON 定义示例。相对 `cwd` 从项目根目录解析，`argv` 是参数数组，`listener` 用于等到该服务的进程组开始监听后再标为就绪。

```json
{
  "cwd": "server/UnifiedInterfaceManagement",
  "argv": [".venv/Scripts/python.exe", "-B", "-m", "http.server", "8099", "--bind", "127.0.0.1"],
  "depends_on": [],
  "listener": true
}
```

在本地控制台输入 `register my-service C:\path\my-service.json` 后即可用名称管理它。服务凭据继续通过各服务自己的配置文件提供。

## 在线管理员

退出并重新打开主管理器。候选用户先注册并验证邮箱，在网站「设置 → 账号」复制用户 ID。登录本地主管理器后，用该 ID 授权。

```text
start user-website
admin grant <用户UUID>
```

管理员先在普通网站完成校园身份绑定和资料登记，再用原邮箱和密码登录 `/admin`。首次开通时可用 `cas add <学工号>` 发放校园身份。授权记录、管理会话和审计保存在主管理器私有状态目录的 `online-admin/web-admin.sqlite3`。

| 命令 | 用途 |
| --- | --- |
| `admin grant <用户UUID>` | 查询 PostgreSQL，向已启用且已验证邮箱的用户授予管理权限 |
| `admin revoke <用户UUID>` | 撤销管理权限和现有管理会话，普通账号仍可登录 |
| `admin list` | 查看已授权的邮箱与 UUID |
| `admin verify-user name@example.com` | 人工核对普通用户的邮箱归属后标记已认证，并使旧的邮箱验证令牌失效 |

管理台通过本机命名管道读取所有登记的进程。MINTA 网关、各模拟后端、教师凭据、RAG 查询、MCP 和独立 Worker 可按主管理器的依赖检查启动、停止或重启。管道只接收由受管统一接口的 HTTP 监听进程提交的固定操作。 [管理台范围与使用](../server/UserWebsite/README.md#在线管理台)

升级校园资料和服务开放范围功能时，先在已运行的 PostgreSQL 上执行 MainAgent 的 `009` 和 `010` 迁移。退出并重新打开主管理器，使本地 CAS 收到内部状态校验凭据，再执行 `start mintagw` 和 `start user-website`。 [数据库迁移命令](../server/MainAgent/main_agent/cloud/README.md#安装与迁移)

## 对外服务管理

在项目的 `MainProcessManager` 目录放置 `caddy.exe`，或将 Caddy 加入 `PATH`，然后登录主管理器。用 `ipconfig` 查看本机 Wi-Fi 或以太网的内网 IPv4。 [Caddy Windows 下载](https://caddyserver.com/download)

### 局域网

```text
external configure website 192.168.42.10
external start website
external status
```

把示例 IP 换成当前网卡地址。Agent 网站默认使用 `https://<内网IPv4>:8443/`，CAS 登录默认使用 `https://<内网IPv4>:8444/`。可在 IP 后依次指定网站 HTTPS 端口、本机网站端口和 CAS HTTPS 端口。普通 `start user-website` 仍提供本机 8082 入口。

在主管理器执行 `start core` 和 `start mintagw` 可同时启动 RAG、MCP 和校园应用的后端服务。

| 控制台命令 | 作用 |
| --- | --- |
| `external status` | 查看网站与 CAS 的局域网地址、代理和网站进程状态 |
| `external stop website` | 关闭两个内网 HTTPS 入口及其专用的网站后端 |
| `external logs website` | 查看代理与网站各自的日志路径 |
| `external cert website` | 查看可分发给客户端的 Caddy 根证书路径 |
| `external firewall website` | 显示仅放行当前入口地址、TCP 端口和本地子网的 PowerShell 命令 |

首次开放时，在管理员 PowerShell 执行 `external firewall website` 输出的命令，放行本地子网到两个 HTTPS 端口。访问设备连同一局域网，导入并信任 `external cert website` 指向的 `root.crt`，再打开 `external status` 显示的 Agent 网站。网卡 IP 变化时，停止入口、保存新地址并启动，同时更新 Windows 防火墙规则。 [Windows 防火墙](https://learn.microsoft.com/powershell/module/netsecurity/new-netfirewallrule) · [Caddy 本地 HTTPS](https://caddyserver.com/docs/automatic-https#local-https)

### 两条公网 FRP 隧道

给 Agent 网站和 CAS 各准备一个 HTTPS 域名，将两个域名解析到运行 `frps` 的服务器。以下是主管理器命令，替换示例域名后执行。

```text
external configure frp https://agent.example.com https://cas.example.com
external start frp
external status frp
```

| 隧道 | 浏览器入口 | 本机 `frpc` 转发目标 |
| --- | --- | --- |
| Agent | `https://agent.example.com` | `127.0.0.1:8483` |
| CAS | `https://cas.example.com` | `127.0.0.1:8484` |

如果自建 `frps`，在 `frps.toml` 设置 `vhostHTTPSPort = 443`。在与主管理器同一台电脑运行 `frpc`，为两个域名分别填入浏览器信任的证书和私钥，或使用覆盖两者的一份证书。保留原始 Host，供网站校验入口和 CAS 匹配回调。 [FRP HTTPS 转发配置](https://gofrp.org/en/docs/examples/https2http/)

```toml
serverAddr = "edge.example.com"
serverPort = 7000
auth.method = "token"
auth.token = "替换为 frps 配置中的令牌"

[[proxies]]
name = "campus-agent"
type = "https"
customDomains = ["agent.example.com"]
[proxies.plugin]
type = "https2http"
localAddr = "127.0.0.1:8483"
crtPath = "./agent-fullchain.pem"
keyPath = "./agent-privkey.pem"

[[proxies]]
name = "campus-cas"
type = "https"
customDomains = ["cas.example.com"]
[proxies.plugin]
type = "https2http"
localAddr = "127.0.0.1:8484"
crtPath = "./cas-fullchain.pem"
keyPath = "./cas-privkey.pem"
```

主管理器为内网和 FRP 分别登记 Agent 与 CAS 的地址配对。CAS 登录和邮箱绑定都回到发起操作的网站地址。CAS 管理台与票据校验端点只通过本机 CAS 端口访问。配置和审计保存在 `control.sqlite3`，`reload` 会重启已运行的入口，退出主管理器会关闭它们。 `external stop frp` 可单独停止两个 FRP 本机目标。 [校园 SSO 配置](../server/MainAgent/main_agent/cloud/SSO.md)

## 逐次请求鉴权

用 `start unified-interface` 启动受管 HTTP 入口。统一入口处理每个请求时，经本机命名管道把方法和已匹配的路由模板交给主管理器，主管理器核对管道客户端 PID 是否属于其启动的 `unified-interface`、`main-agent` 或 `main-worker` 进程组。校验通过即放行该次请求。每个后续请求重新校验。 [Windows 命名管道本机限制](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)

受管 MainAgent 或 Worker 也可启动统一接口管理器的 `main.py local` 子进程，通过 STDIO 直接提交实际请求。子进程继承主管理器鉴权通道，执行同一套逐次校验。用户令牌、管理员凭据和资源权限仍由统一入口及相应业务服务校验，鉴权管道只传方法和路由模板。 [统一接口管理器的 STDIO 协议](../server/UnifiedInterfaceManagement/README.md#本地-stdio)

| 状态 | 含义 |
| --- | --- |
| `403 supervisor_denied` | 本次调用的进程身份未通过主管理器鉴权 |
| `503 supervisor_unavailable` | 本机鉴权通道暂不可用 |

## 验证

```powershell
.\server\UnifiedInterfaceManagement\.venv\Scripts\python.exe -B -m unittest discover -s MainProcessManager/tests -v
```
