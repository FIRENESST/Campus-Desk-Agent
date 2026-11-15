# 统一接口管理器

HTTP 和本地标准输入输出（STDIO）使用同一份路由和权限配置。一般用户入口沿用 MainAgent 的 `/v1` 路径，管理和服务调用集中在 `/admin`。用户、实例、会话和任务由 MainAgent 认证并按账号隔离。路由清单可分别通过 `GET /v1/interfaces` 和 `GET /admin/interfaces` 获取。

## 数据库分工

| 数据库 | 保存内容 | 管理器入口和配置 |
| --- | --- | --- |
| **PostgreSQL** | 用户资料、邮箱与校园身份、登录会话、访问令牌记录、用户实例、对话消息、任务 | 用户走 MainAgent 的 `/v1/auth/*`、`/v1/me`、`/v1/instances/*`、`/v1/conversations/*`、`/v1/runs/*`。管理员可查 `GET /admin/postgres/ready`。管理器的 `UIM_MAIN_URL` 指向 MainAgent，数据库连接由 MainAgent 的 `CLOUD_DATABASE_URL` 配置 |
| **Milvus RAG** | 资料来源、文本分段和向量 | 用户走 `POST /v1/rag/search`，管理员走 `/admin/rag/*`。检索走 `UIM_RAG_URL`，资料管理使用 RAG 的 Milvus 地址和集合配置 |

一般用户检索时，管理器先向 MainAgent 验证访问令牌，随后只将 `query`、`scope`、`top_k` 发送到 RAG。接口清单的 `data_domain` 区分 `postgresql`、`milvus`、MINTA 模拟数据、校园文件和路由反馈的本机 SQLite。

## Windows 启动

进入 `server/UnifiedInterfaceManagement`。首次部署时创建虚拟环境并准备配置。

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

将下面命令生成的值写到 `.env` 的 `UIM_ADMIN_TOKEN`。需要给主智能体配置独立模型凭据时，再生成一个值写到 `UIM_LLM_TOKEN`。

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

按各服务的说明启动所需上游。默认地址是 MainAgent `127.0.0.1:8081`、RAG 查询 `127.0.0.1:8002`、MCP `127.0.0.1:8001/mcp`、MINTA `127.0.0.1:19000`。llmServer 端口优先取其 `.env` 的 `LOCAL_PORT`，缺省为 `8000`。RAG 启动器换用其他端口时，将实际地址写入 `UIM_RAG_URL`。本机 RAG 资料管理和 MainAgent 路由管理通过各服务的 Python 环境操作。

```powershell
.\.venv\Scripts\python.exe -B main.py serve
```

在另一个终端检查服务和路由清单。

```powershell
Invoke-RestMethod http://127.0.0.1:8090/health
Invoke-RestMethod http://127.0.0.1:8090/v1/interfaces
```

HTTP 默认监听 `127.0.0.1:8090`。可用 `--host`、`--port` 修改监听地址，用 `--env-file` 选择配置文件。系统环境变量优先于管理器配置文件。公网入口使用 HTTPS，`UIM_PUBLIC_URL` 设为外部访问源。由反向代理终止 TLS 时，保留外部 `Host`，重设 `X-Forwarded-Proto` 和 `X-Forwarded-For`，并使用 `--trusted-proxies` 指定代理 IP 或网段。也可使用 `--ssl-certfile` 和 `--ssl-keyfile` 直接提供证书。部署统一入口时，将上游服务限制在本机或受控内网。

使用自建 CAS 时，主管理器会登记局域网和 FRP 网站对应的 CAS 地址。服务票据由 MainAgent 向本机 CAS 校验。外部身份源使用 MainAgent 的 `CLOUD_PUBLIC_URL`。SSO 回调的查询参数、表单和浏览器绑定 Cookie 会随请求转发。校园街景路径保持同源，照片和视频支持按字节范围读取。 [MainAgent 云 API 与认证](../MainAgent/main_agent/cloud/README.md) · [校园 SSO 配置](../MainAgent/main_agent/cloud/SSO.md)

## 网站服务器接入

项目内的普通用户网站已通过 HTTP 接入。 [网站启动与配置](../UserWebsite/README.md)

网站后端以统一管理器的地址为 API 基址。同机开发时使用 `http://127.0.0.1:8090`。跨主机访问或使用生产模式的 MainAgent 时，使用配置好的 HTTPS 入口。网站业务经 MainAgent 读写 PostgreSQL，资料检索经 RAG 访问 Milvus。

用户请求使用各自的 `Authorization: Bearer <access_token>`。JSON 请求发送 `Content-Type: application/json`，返回 `204` 时响应体为空。管理员和服务凭据由需要它们的后端保管。

聊天接入顺序是登录 → 获取或创建实例 → 获取或创建会话 → 提交运行 → 查询运行状态 → 读取会话消息。提交运行返回 `202` 和运行对象，用其中的 `id` 查询 `GET /v1/runs/{run_id}`。运行终态为 `succeeded`、`failed` 或 `cancelled`，业务结果看 `result_status`。最终回答在消息列表的 `role:"assistant"` 条目中，可用消息的 `run_id` 与本次运行对应。

## 在线管理接口

网站的 `/admin` 使用同源的 `/admin/api/*`。网站先通过 MainAgent 核验邮箱密码，再将已登录的身份交给本机统一入口。统一入口从 MainAgent 获取用户 UUID 并核验主管理器登记的授权，每次管理请求校验邮箱登录和绑定的管理会话。MCP 和 MainAgent 的运行状态读取使用单独的本机状态凭据。

| 接口 | 用途 |
| --- | --- |
| `POST /admin/api/login`、`GET /admin/api/session`、`POST /admin/api/logout` | 管理员登录、会话读取和退出 |
| `GET /admin/api/data/{resource}` | 业务数据分页。用户支持按 ID、邮箱或名称查询，消息按会话读取 |
| `/admin/api/data/{resource}/{id}`、`/admin/api/data/{resource}/{id}/revoke` | 资源管理与登录会话撤销 |
| `/admin/api/rag/*`、`/admin/api/routing/*`、`/admin/api/credentials/*` | RAG、路由资料和教师凭据 |
| `GET /admin/api/cas/profiles`、`PATCH/DELETE /admin/api/cas/profiles/{id}` | 查询绑定与资料，带版本核验资料或解除绑定 |
| `POST /admin/api/cas/profiles/bulk-review` | 提交本页 1 至 40 个待核验的 `{id,revision}` 至 `items`，批量通过学工号匹配且已启用的本地 CAS 资料。资料版本或账号状态变化时整批退回 |
| `GET/PUT /admin/api/campus/rules/{service}`、`PUT /admin/api/campus/rules/classroom/{id}` | 维护服务及单个教室的开放范围和教室手工占用 |
| `GET /admin/api/campus/clubs/review`、`PATCH /admin/api/campus/clubs/review/{id}` | 查看社团审核队列，带版本通过或撤下社团 |
| `GET /admin/api/campus/clubs/archives?offset=0`、`PATCH /admin/api/campus/clubs/archives/{id}` | 分页查看全站归档。提交 `{action:"restore" 或 "purge",version,activity_id?}` 恢复或永久删除，路径 ID 为所属社团 |
| `GET /admin/api/campus/photos/places?status=pending&offset=0` | 分页列出有匹配投稿的地点和照片数，`status` 可取 `all`、`pending`、`approved` 或 `rejected` |
| `GET /admin/api/campus/photos/review?place={place}&status=pending&offset=0` | 分页列出指定地点的照片，省略 `place` 可查询所有点位 |
| `GET /admin/api/campus/photos/review/{id}/image`、`PATCH /admin/api/campus/photos/review/{id}` | 预览原投稿的处理后 JPEG，提交 `{status,version}` 通过、驳回或撤下照片 |
| `DELETE /admin/api/campus/photos/review/{id}?version={version}` | 永久删除指定照片及图片内容。照片版本变化后须刷新 |
| `/admin/api/runtime/processes`、`/admin/api/runtime/processes/{name}` | 进程查询和已授权的操作提交 |
| `/admin/api/runtime/skills`、`/admin/api/runtime/tools`、`/admin/api/minta/readyz` | 技能、MCP 工具和 MINTA 上游状态 |
| `GET /admin/api/audit` | 最近的管理审计 |

服务凭据接口仍由原有目录列出，在线管理台的资源以此处的浏览器接口为准。 [管理台使用](../UserWebsite/README.md#在线管理台)

## 本机主管理

通过 `MainProcessManager` 的 `start unified-interface` 启动受管 HTTP 入口。受管 MainAgent 启动的 STDIO 子进程也会继承主管理器的鉴权通道。统一入口在转发每次请求前经本机命名管道校验调用进程，结果只用于本次请求。进程身份不符时返回 `403 supervisor_denied`，鉴权通道不可用时返回 `503 supervisor_unavailable`。业务凭据和资源权限由原接口校验。 [主管理器逐次鉴权](../../MainProcessManager/README.md#逐次请求鉴权)

## 一般用户

表内 `?` 表示可选字段。登录前可使用发现、邮箱认证和校园 SSO 登录入口。

### 发现与邮箱认证

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /health` | — | `200` 管理器存活，含 `status`、`service` |
| `GET /ready` | — | `200` MainAgent PostgreSQL 就绪 |
| `GET /v1/interfaces` | — | `200 {interfaces}`，条目含 `path`、`methods`、`access`、`service`、`data_domain` |
| `GET /v1/auth/providers` | — | `200 {providers,supported_sso_protocols}` |
| `POST /v1/auth/register` | `{email,password,display_name?}` | `202 {message}` |
| `POST /v1/auth/verification/request` | `{email}` | `202 {message}`，受理重发验证邮件 |
| `POST /v1/auth/verification/confirm` | `{token}` | `204`，验证邮箱 |
| `POST /v1/auth/login` | `{email,password,remember_me?}` | `200`，返回令牌对。选择记住登录时增加 `remember_token` 和 `remember_expires_in` |
| `POST /v1/auth/refresh` | `{refresh_token}` | `200`，返回轮换后的令牌对 |
| `POST /v1/auth/remember` | `{token}` | `200 {access_token,token_type,remember_expires_in}`，由网站后端恢复登录 |
| `POST /v1/auth/password/request` | `{email}` | `202 {message}`，受理密码重置邮件 |
| `POST /v1/auth/password/reset` | `{token,new_password}` | `204`，重置密码并撤销旧登录会话 |

登录的令牌响应含 `access_token`、`refresh_token`、`token_type`、`expires_in`（秒）、`session_id`。新密码长度为 8–128 个字符，至少包含一个数字和一个小写英文字母。邮箱验证令牌有效 10 分钟。同类邮件重发间隔至少 60 秒，冷却响应 `429 email_cooldown` 带 `Retry-After`。记住登录的恢复令牌最长有效 30 天。

### 校园 SSO

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `POST /v1/auth/sso/campus/start` | `{code_challenge,mode?,current_password?,website_origin?}`。`mode` 默认 `login`，`link` 需要当前用户 Bearer 和密码。自建 CAS 的已登记网站可提交 `website_origin` | `200 {authorization_url,expires_in}`，并设置浏览器绑定 Cookie |
| `GET /v1/auth/sso/campus/callback` | OIDC 或 CAS 从浏览器回传查询参数 | `200 {completion_code,expires_in,mode}` |
| `POST /v1/auth/sso/campus/callback` | SAML 回传表单 `SAMLResponse`、`RelayState` | `200 {completion_code,expires_in,mode}` |
| `POST /v1/auth/sso/campus/finish` | `{completion_code,code_verifier}` | `200`，返回令牌对或 `{linked:true}` |
| `GET /v1/auth/sso/campus/metadata` | SAML 配置启用时供校方读取 | `200`，SAML XML |

网站代理 SSO 时，浏览器在同一网站源完成 `start` 和回调，代理须转发 `Set-Cookie` 和回调的 `Cookie`。网站用自己为本次登录生成的 PKCE verifier 兑换完成码。回调路径为 `/v1/auth/sso/campus/callback`，其源地址取自已登记的自建 CAS 网站配对或外部身份源的 `CLOUD_PUBLIC_URL`。回调本身返回 JSON。 [PKCE、协议配置与身份绑定](../MainAgent/main_agent/cloud/SSO.md)

### 账号、实例、对话和运行

以下接口需要用户访问令牌。实例、会话和任务 ID 为 UUID，所属用户由令牌确定。

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /v1/me` | — | `200`，包含账号信息、登记姓名和校园资料状态 `campus_status` |
| `GET /v1/profile`、`PUT /v1/profile` | 保存时提交 `{display_name,full_name,college,grade}`，使用外部身份源时还需提交 `campus_number` | `200`，读取或保存当前用户的资料，教师的 `grade` 为 `null`。本地 CAS 绑定的资料查询返回 `verified_campus_number`，保存时由服务端填写学工号 |
| `GET /v1/auth/identities` | — | `200 {items}`，已绑定校园身份 |
| `DELETE /v1/auth/identities/{identity_id}` | `{current_password}` | `204`，解绑校园身份 |
| `POST /v1/auth/password/change/request` | `{old_password}` | `202 {message}`，向账号邮箱发送有效 10 分钟的改密令牌 |
| `POST /v1/auth/password/change` | `{old_password,new_password,token}` | `204`，在同一登录会话确认邮箱令牌，修改密码并撤销旧登录会话 |
| `GET /v1/auth/sessions` | — | `200 {items}`，有效设备登录会话 |
| `DELETE /v1/auth/sessions/{session_id}` | — | `204`，撤销指定设备会话 |
| `POST /v1/auth/logout` | — | `204`，退出当前设备 |
| `POST /v1/auth/logout-all` | — | `204`，退出所有设备 |
| `GET /v1/instances` | — | `200 {items}`，当前用户的实例 |
| `POST /v1/instances` | `{name}`，最长 100 字符 | `201`，实例对象 |
| `GET /v1/instances/{instance_id}` | — | `200`，实例对象 |
| `PATCH /v1/instances/{instance_id}` | `{name?,status?,is_default?}` 至少填一项。状态为 `active` 或 `paused`，设为默认时传 `is_default: true` | `200`，更新后的实例对象 |
| `DELETE /v1/instances/{instance_id}` | — | `204`，永久删除实例和下属数据 |
| `GET /v1/instances/{instance_id}/conversations` | 查询参数 `limit?`、`cursor?`、`q?`。 `q` 按标题搜索，最多 200 字符 | `200 {items,next_cursor}`，会话倒序 |
| `POST /v1/instances/{instance_id}/conversations` | `{title?}`，可传 `{}`，默认「新会话」，最长 200 字符 | `201`，会话对象 |
| `PATCH /v1/conversations/{conversation_id}` | `{title}`，最长 200 字符 | `200`，更新后的会话对象 |
| `DELETE /v1/conversations/{conversation_id}` | — | `204`，永久删除会话和下属数据 |
| `GET /v1/conversations/{conversation_id}/messages` | 查询参数 `limit?`、`cursor?` | `200 {items,next_cursor}`，每页按消息 ID 升序 |
| `POST /v1/conversations/{conversation_id}/runs` | `{query,targets?}`，必带请求头 `Idempotency-Key` | `202`，含 `id`、`status` 的运行对象 |
| `GET /v1/runs/{run_id}` | — | `200`，运行对象、正在执行的子智能体 ID 列表 `active_agents`、`steps` 和本轮 `messages` |
| `POST /v1/runs/{run_id}/cancel` | — | `200`，请求取消后的运行对象，正在执行的任务可能尚未进入终态 |
| `POST /v1/runs/{run_id}/retry` | `{attempt}`，带 `Idempotency-Key` | `202`，重新执行当前会话的最新一轮 |
| `GET /v1/runs/{run_id}/withdrawal` | — | `200`，返回最新一轮的 `{attempt,running,operations}` 供逐项确认 |
| `POST /v1/runs/{run_id}/withdrawal` | `{attempt,approvals}` | `204`，撤销已确认的操作并撤回最新一轮 |

实例对象含 `id`、`user_id`、`name`、`profile`、`status`、`is_default` 和创建、更新时间。列表优先返回账号自己的默认实例，默认实例计入账号上限。会话对象含 `id`、`user_id`、`instance_id`、`title` 和创建、更新时间。消息条目含 `id`、`conversation_id`、`run_id`、`role`、`content`、`created_at`。两种分页的 `limit` 默认 50，范围 1–100。网站将上页返回的 `next_cursor` 原样传回即可获取更早内容。

同一账号在所有实例中最多同时处理三个会话。排队与运行中的任务共用这三个名额，新提问和重试超过上限时返回 `429 run_limit`。

实例和会话内有正在执行的任务时，删除会返回 `409 run_in_progress`。删除最后一个实例后，重新读取实例列表可取得新建的空默认实例。

`query` 最长 12,000 字符。`targets` 可建议 `campus_introduction`、`information_search`、`campus_services`、`rumor_assessment`，最多四项且互不重复。MainAgent 决定是否委派并组织最终回复。`Idempotency-Key` 为 1–128 位无空白 ASCII 字符。一次提交若需要重试，应复用原键和原请求体。 [MainAgent API 与会话规则](../MainAgent/main_agent/cloud/README.md#api)

### 校园传闻和辟谣

完成校园身份绑定和资料登记后，`GET /v1/campus-rumor` 返回 `{pending,confirmed}`。`POST /v1/campus-rumor/tips` 接受 `{content}`，填写 5 到 2000 字，成功时返回 `201 {ok,tid}`。网站 `/admin/rumor` 复用在线管理员会话访问原谣言防控管理台。 [服务启动与入口](../CampusRumor/README.md)

### 校园街景与 RAG

以下接口需要用户访问令牌。

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /v1/campus-view` | — | `200`，校园信息、点位摘要 `points` 和路线摘要 `routes` |
| `GET /v1/campus-view/routes/{route_id}` | 路线 ID | `200`，路线信息、折线 `path` 和按顺序排列的点位 |
| `GET /v1/campus-view/points/{point_id}` | 点位 ID | `200`，点位详情及 `content` 内容块 |
| `POST /v1/campus-view/resolve` | `{point,route_id?}` 或 `{longitude,latitude,radius_m?,route_id?}`。空对象进入浏览 | `200`，`status` 为 `found`、`ambiguous`、`no_match`、`route_not_found` 或 `browse` |
| `GET /v1/campus-view/media/{name}` | 媒体名称，可带 `Range` | `200` 文件或 `206` 部分内容 |
| `GET /v1/campus-view/photo-places` | — | `200 {items}`，已发布的照片点位及照片数 |
| `GET /v1/campus-view/photo-places/{place}/photos` | `place` 为 WGS84 量化点位 ID，`offset` 默认 0 | `200 {items,next_offset}`，每页最多 12 张已发布照片 |
| `POST /v1/campus-view/photo-places/{place}/photos` | `application/octet-stream` 图片及 `Idempotency-Key`，单张最多 20 MB（20,971,520 字节） | `201`，提交待审核照片 |
| `GET /v1/campus-view/photos/{photo_id}/image` | 已发布的照片 ID | `200 image/jpeg` |
| `POST /v1/rag/search` | `{query,scope?,top_k?}`，`scope` 限已开放的 `UIM_RAG_SCOPE` | `200 {hits}` |

街景坐标采用 WGS84，数组顺序为经度、纬度。按坐标定位的 `radius_m` 默认为 40，范围 1–150 米。点位的 `content` 按 `kind` 区分 `text`、`image`、`panorama`、`video`、`link` 和 `custom`。媒体使用返回的 `url`，自定义块以 `component` 和 `props` 交给网站渲染。媒体请求同样带用户凭据，网站后端代理媒体时可转发 `Range`、`Content-Range` 和对应状态码。 [街景数据和内容配置](../../DataBase/CampusNavigation/README.md)

地点照片以约 10 米的网格聚合。点位 ID 为四舍五入后的 `经度×10000_纬度×10000`。上传支持静态 JPEG、PNG 或 WebP，最大 6 MiB。服务会去除图片元数据并保存 JPEG，公共相册只返回审核通过的照片。

RAG `query` 最长 4,000 字符，`top_k` 默认为 5，范围 1–50。省略 `scope` 时使用配置的开放范围，默认 `campus`。命中条目含 `chunk_id`、`document_id`、`chunk_index`、`source`、`text` 和 `score`，空结果为 `{"hits":[]}`。

### 公众号总成

两项接口使用普通用户登录会话，并核验校园身份登记状态。日报服务默认监听本机 `8184`，自定义端口时使用与受管日报相同的 `CAMPUS_DIGEST_PORT`。

| 请求 | 说明 |
| --- | --- |
| `GET /v1/campus-digest/wechat?title=志愿活动&account=学生会&offset=0` | 可选 `title` 仅匹配标题，`account` 支持公众号名称或登记别名片段，两项可组合。`offset` 用于翻页。返回 `items`、`page_size`、`has_more`、`revision` 和 `stale`。文章条目含 `published_on` 和可用时的 `published_at` |
| `GET /v1/campus-digest/wechat/events` | SSE，连接时返回当前版本，每次索引更新推送新版本。连接定期重建并重新鉴权 |

### MINTA 校园应用

以下只读接口可通过 HTTP 或本地 STDIO 调用。统一入口向 MainAgent 校验 PostgreSQL 登录会话，受管部署也由主管理器逐次鉴权。用户访问令牌留在认证链路内。网站可以直接读取日程数据，和聊天流程独立。

| 请求 | 查询参数 | 成功响应 |
| --- | --- | --- |
| `GET /v1/minta/schedule` | `week` 可选，范围 1–20，默认 1 | `200 {source,week,audience,periods,items}`。`audience` 为 `student` 或 `teacher`。`periods` 包含 `period`、`start_time`、`end_time`。课程含 `course_id`、`course_name`、`day`、`start_period`、`end_period`、`classroom`、`teacher`、`points`，有对应时段时附上 `start_time`、`end_time` |
| `GET /v1/minta/activities` | 无 | `200 {source,items}`。活动含 `id`、`name`、`description`、`location`、`starts_at`、`ends_at`、`capacity`、`remaining`、`points`。起止时间精确到分钟，省略时区时按北京时间 |
| `GET /v1/minta/semester-report` | 无，使用当前登录账号 | `200 {source,term,audience,courses,activities,total_points}`。两个明细各有 `{status,items,points}`。课程提供 `grade`（模拟成绩）和 `points`（积分），活动提供 `id`、`name`、`starts_at`、`points`。明细无法读取时，该项小计及总积分为 `null` |
| `GET /v1/minta/classrooms` | 无 | `200 {source,items}`。教室含 `room_id`、`name`、`capacity`、`reserved` |

这组接口的 `source` 均为 `minta_simulation`。课表由模拟排课服务读入本地数据，学生和教师的课表分别按已核验的学号及工号查询。身份未核验时，响应的 `student_verified` 或 `staff_verified` 为 `false`，`items` 为空。个人日程把可见活动和课表按北京时间合并。学期起始日通过 `CLOUD_TERM_START` 配置，学期报告覆盖 20 周，每门课程及仍有效的活动报名各计一次积分。教室接口提供当前活跃预约的布尔状态。主管理器执行 `start mintagw` 会启动模拟服务。

#### 日历同步

| 请求 | 鉴权与响应 |
| --- | --- |
| `GET /v1/calendar/events?start=YYYY-MM-DD&end=YYYY-MM-DD` | 已绑定校园身份的登录会话。按北京时间返回与指定区间重叠的本人自定义事件及教室预约。`end` 为区间后一日的零点边界，范围最多 31 天。结果 `{items, classroom_unavailable}` |
| `POST /v1/calendar/events` | 提交 `{id,title,location?,starts_at,ends_at}`，`id` 为客户端生成的 UUID。同一账号用相同 ID 和相同内容重试时返回同一事件。响应状态 `201` |
| `PUT /v1/calendar/events/{event_id}` | 修改本人自定义事件。提交 `{title,location?,starts_at,ends_at,revision}`，成功返回新版事件 |
| `DELETE /v1/calendar/events/{event_id}?revision=1` | 删除本人对应版本的自定义事件。成功返回 `{deleted:true}` |
| `GET /v1/calendar/subscriptions` | 登录会话。返回本人订阅的 `{items:[{id,label,created_at}]}` |
| `POST /v1/calendar/subscriptions` | 登录会话及校园身份。提交 `{label}`。返回订阅信息及一次性显示的相对网址 `path` |
| `DELETE /v1/calendar/subscriptions/{subscription_id}` | 登录会话。按当前账号撤销对应订阅，返回 `204` |
| `GET/HEAD /v1/calendar/export.ics` | 登录会话及校园身份。返回 `text/calendar` 附件 |
| `GET/HEAD /v1/calendar/feed.ics` | 使用订阅专用 Bearer 凭据。网站的 `/calendar/{凭据}.ics` 会把链接转换成此请求。返回 `text/calendar`，支持 `If-None-Match` |
| `GET /v1/calendar/microsoft-todo` | 登录会话。返回功能配置和本人连接状态 |
| `POST /v1/calendar/microsoft-todo/start` | 登录会话及校园身份。启动绑定当前会话的微软授权 |
| `POST /v1/calendar/microsoft-todo/finish` | 登录会话及校园身份。网站回调代理提交 `{state,code}`，一次性完成授权 |
| `POST /v1/calendar/microsoft-todo/sync` | 登录会话及校园身份。安排本人日程同步，返回 `202` |
| `DELETE /v1/calendar/microsoft-todo` | 登录会话。断开本人的微软连接，返回 `204` |

以下接口使用普通用户登录会话。办理请求通过 MainAgent 校验业务角色、教师凭据及记录归属，再经 MINTA 发往对应服务。

日程的 `kind` 为 `custom` 或 `classroom`。自定义事件有 `revision`，修改或删除时版本过期会返回 `409 calendar_event_changed`。起止时间精确到分钟，省略时区时按北京时间。教室预约通过原校务接口取消。

| 请求 | 输入与返回 |
| --- | --- |
| `GET /v1/minta/catalog/{service}` | `service` 为 `classroom`、`gym`、`club` 或 `library`。教室传 `date=YYYY-MM-DD` 可查北京时间单日占用，每间教室的 `busy_slots` 返回 `{start_minute,end_minute}` 区间。`[]` 表示暂无预约，`null` 表示暂不可用。体育馆附带七天可预约范围，传 `venue_id` 和 `date` 可查该场馆单日的编号、开始时间、结束时间与匿名余量。馆藏支持 `q`（书名、作者或编号）、`category`（类别编号或名称）和 `page`（从 1 开始） |
| `GET /v1/minta/operations?service=library` | `service` 也可为 `classroom`、`gym` 或 `club`。返回本人的 `{items}` |
| `POST /v1/minta/operations` | 请求头 `Idempotency-Key`。请求体 `{service,resource_id}`。教室预约带上北京时间 `starts_at` 和 `ends_at`，学生另加 `teacher: {name,phone}`。体育馆需附目录给出的 `slot_id` 和 `identity_confirmed: true`。借书可加整数 `duration_days`，范围 1 至 14，省略时为 7 |
| `DELETE /v1/minta/operations/{operation_id}` | 取消本人预约或报名，或归还本人图书（含提前归还）。使用创建操作返回的 `id` |
| `GET /v1/minta/teacher-credential` | 教师读取本人凭据，返回 `{credential}`。凭据无效时值为 `null` |
| `GET /v1/minta/clubs` | 返回可见的社团、登记人、活动、当前账号的 `can_manage` 和归档清单摘要 `archive_summary` |
| `POST /v1/minta/clubs` | 请求头 `Idempotency-Key`。`action` 可为 `register`、`update`、`archive`、`restore`、`publish_activity`、`update_activity`、`archive_activity`、`restore_activity`、`purge_archive`、`purge_archives` |

社团登记提交 `name`、`category`、`description`。修改提交这些字段以及 `id`、`version`，归档只提交 `id`、`version`。发布活动提交社团 `id` 和 `name`、`description`、`location`、`starts_at`、`ends_at`、`capacity`，修改活动另带 `activity_id`、活动 `version`，归档活动只提交这三个标识字段。时段精确到分钟，省略时区时按北京时间，结束必须晚于开始。登记账号由服务端确定。

清空归档提交 `{action: "purge_archives", snapshot}`。从当前账号的 `archive_summary` 读取 `snapshot`、`club_count` 和 `activity_count`。确认后永久删除该账号名下的已归档社团及其活动，以及其余自有社团中已归档的活动。关联报名随之结束。成功返回实际删除的 `{club_count, activity_count}`，清单变动时返回 `409 club_archives_changed`，需重新获取清单后确认。

单项归档恢复和删除提交 `id` 及当前 `version`，活动另外提交 `activity_id` 和活动的 `version`。版本变化时重新读取归档。恢复后沿用当前审核状态。社团只有审核状态为 `approved` 时才能提交 `publish_activity`。

预约、报名和借还操作响应包含 `id`、`service`、`resource_id`、`resource_name`、`state`、`created_at` 和可选消息。`active` 表示办理成功，`closed` 表示已取消或归还。`pending`、`uncertain`、`closing`、`close_uncertain` 等待查询核对，HTTP `202` 时请刷新个人记录。`failed` 表示服务拒绝。相同提交应重用原 `Idempotency-Key`。已确认的个人借阅回执和馆藏的每项 `loan_progress` 都有 `duration_days`、`borrowed_at` 和 `due_at`。馆藏进度公开给可访问目录的校园账号，借阅记录和归还操作归属本人。

带查询参数的馆藏请求每页返回 8 种图书。无参数时返回完整馆藏。每本书带有 `category_id`、`category`、库存和匿名 `loan_progress`。`categories` 和 `summary` 描述整个馆藏，`total` 为当前筛选结果的总数。

## 管理员与服务调用

以下接口默认使用 `Authorization: Bearer <UIM_ADMIN_TOKEN>`。模型调用和 MCP 的凭据在表中单独标明。管理 HTTP 默认限本机，远程调用需要 HTTPS 及 `UIM_ALLOW_REMOTE_ADMIN=true`。

### 检查与 RAG 资料管理

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /admin/interfaces` | — | `200 {interfaces}`，含管理和服务路由 |
| `GET /admin/main/health` | — | `200`，MainAgent 存活 |
| `GET /admin/postgres/ready` | — | `200`，通过 MainAgent 确认 PostgreSQL 就绪 |
| `GET /admin/rag/health` | — | `200`，RAG 查询服务就绪 |
| `GET /admin/rag/scopes` | — | `200 {scopes}`，现有资料范围 |
| `POST /admin/rag/search` | `{query,scope?,top_k?}`，可指定其他资料范围 | `200 {hits}` |
| `GET /admin/rag/documents` | 查询参数 `scope?` | `200 {scope,documents}`，每条含 `document_id`、`source` |
| `POST /admin/rag/documents` | 文本 `{scope?,source,text}` 或文件 `{scope?,filename,data_base64,source?}` | `200 {scope,source,document_id,chunks}`，`chunks` 是片段数量 |
| `GET /admin/rag/documents/{document_id}` | 查询参数 `scope?` | `200 {scope,document_id,chunks}`，`chunks` 是正文片段数组 |
| `DELETE /admin/rag/documents/{document_id}` | 查询参数 `scope?` | `200 {scope,document_id,deleted}` |

导入文件上限 20 MiB，支持文本、DOCX 和带文本层的 PDF。粘贴正文与上传文件的 JSON 请求体上限均为 32 MiB，正文自动分片。文档 ID 由导入返回，为 64 位小写十六进制字符串。默认资料范围为 `UIM_RAG_SCOPE`，跨范围管理时显式指定。自定义 Milvus 时，填写与 RAG 查询进程相同的 `RAG_MILVUS_URI`、`RAG_COLLECTION`、`RAG_MILVUS_TOKEN`。 [RAG 文件格式与查询服务](../../DataBase/RAG/README.md)

### 路由反馈

| 请求 | 输入 | 成功响应 |
| --- | --- | --- |
| `GET /admin/routing/status` | — | `200 {enabled}` |
| `PUT /admin/routing/status` | `{enabled}`，布尔值 | `200 {enabled}` |
| `GET /admin/routing/overview` | 查询参数 `days?`，默认 14，范围 1–90 | 反馈状态、留存样本统计、按北京时间采集日分组的趋势、派发分布和角色介绍 |
| `GET /admin/routing/introductions` | — | `200`，角色 ID 到角色介绍的映射 |
| `PUT /admin/routing/introductions/{role}` | `{text}`，1–1,200 字符 | `200 {updated}`，值为角色 ID |
| `DELETE /admin/routing/introductions/{role}` | — | `200 {reset}`，恢复默认介绍 |
| `POST /admin/routing/preview` | `{prompt}`，1–800 字符 | `200 {enabled,suggested_targets,suggest_direct,method}`，试算反馈库给出的建议 |
| `GET /admin/routing/samples` | `limit?`、`offset?`、`status?`、`agent?`、`origin?`、`q?`、`day?` | `200 {samples,total,offset,next_offset}`，已脱敏路由样本 |
| `PUT /admin/routing/samples/{sample_id}/grade` | `{score,targets,direct?}`，评分 1–5。`direct: true` 配合空目标数组表示直接回答。仅填空目标数组表示应先澄清 | `200 {graded}`，值为样本 ID |
| `DELETE /admin/routing/samples/{sample_id}/grade` | — | `200 {cleared}`，值为样本 ID |
| `DELETE /admin/routing/samples/{sample_id}` | 样本 ID | `200 {deleted}`，值为样本 ID |
| `POST /admin/routing/samples/bulk-delete` | `{sample_ids}`，一次最多 100 个 | `200 {deleted_count}` |

`limit` 默认 20，范围 1–100。`status` 支持 `all`、`pending`、`reviewed`、`different`。`agent` 按实际结果筛选，支持子智能体 ID、`direct`（直接回答）、`undispatched`（未派发且没有直接回答标记）及 `none`（所有未调用子智能体的样本）。`origin` 支持 `automatic` 和 `preferred`，后者表示用户提供了选角偏好。`day` 使用北京时间的 `YYYY-MM-DD`。样本用 `answered_direct` 记录是否直接回答，用 `expected_direct` 记录标注。样本的 `created_at` 是 Unix 秒，旧样本可为 `null`。

在线 `/admin` 的「路由反馈」通过 `/admin/api/routing/*` 使用同一套管理接口，并继承在线管理员会话和审计。试算中 `suggest_direct: true` 表示建议直接回答，`suggested_targets: []` 表示建议先澄清。两者分别为 `false` 和 `null` 时表示暂无明确建议。`method` 可为 `exact`、`similar`、`semantic`、`conflict` 或 `null`。

在 MainAgent 的 `.env.cloud` 里设置 `CLOUD_ROUTING_DATA_DIR` 后，统一接口默认读取同一个目录。目录须预先创建，并允许两个服务的运行账号读写。配置后在主管理器执行 `reload`，由 MainAgent 初始化或升级路由 SQLite。新库默认关闭反馈采集。 [路由反馈配置](../MainAgent/main_agent/cloud/README.md#本机路由反馈)

### 模型、MCP 与 MINTA

| 请求 | 凭据 | 输入与响应 |
| --- | --- | --- |
| `GET /admin/llm/health` | 管理员 | llmServer 存活检查 |
| `GET /admin/llm/v1/models` | 管理员或 `UIM_LLM_TOKEN` | 转发模型列表 |
| `POST /admin/llm/v1/chat/completions` | 管理员或 `UIM_LLM_TOKEN` | OpenAI 兼容消息生成，支持转发 SSE |
| `POST /admin/llm/v1/completions` | 管理员或 `UIM_LLM_TOKEN` | 转发文本补全 |
| `POST /admin/llm/v1/embeddings` | 管理员或 `UIM_LLM_TOKEN` | 转发向量请求 |
| `POST /admin/llm/v1/responses` | 管理员或 `UIM_LLM_TOKEN` | 转发 Responses 请求 |
| `GET /admin/mcp` | `MAIN_AGENT_MCP_TOKEN` | MCP 事件流 |
| `POST /admin/mcp` | `MAIN_AGENT_MCP_TOKEN` | MCP 初始化和 JSON-RPC 工具调用 |
| `DELETE /admin/mcp` | `MAIN_AGENT_MCP_TOKEN` | 结束 MCP 会话 |
| `GET /admin/minta/healthz` | 管理员 | mintagw 存活检查 |
| `GET /admin/minta/readyz` | 管理员 | mintagw 就绪检查 |
| `POST /admin/minta/v1/call` | 管理员 | `{target,method?,headers?,upstream?,timeout_ms?}` 加可选的 `json`、`body` 或 `body_base64`，返回封装的上游 HTTP 状态及内容 |

同一工作区默认从 llmServer 的 `.env` 读取其 `LOCAL_API_KEY`，并从 MCP 的 `.env` 读取专用凭据。主智能体经管理器访问模型时，将其 `MAIN_AGENT_LLM_SERVER_URL` 设为管理器基址加 `/admin/llm/v1`，`MAIN_AGENT_LLM_SERVER_API_KEY` 设为 `UIM_LLM_TOKEN` 的值。MCP 客户端使用 `connect_main_agent_mcp(url=..., token=...)`，工具清单通过 MCP 的 `tools/list` 获取。 [模型服务](../llmServer/README.md) · [MCP 工具](../mcpServer/README.md)

MINTA 使用 `UIM_MINTA_URL` 指定网关地址，显式留空时停用 MINTA 接入。管理调用的 `target` 使用网关内以 `/` 开头的路径。 [mintagw HTTP/JSON 契约](../../MlNTA/docs/HTTP_GATEWAY.md)

### HTTP 错误处理

网站先按 HTTP 状态处理，再读取错误内容。管理器和 MainAgent 的常见错误体为 `{"error":{"code":"…","message":"…"}}`。RAG 上游也可能返回 `{"error":"…"}`。

| 状态 | 处理场景 |
| --- | --- |
| `401` | 凭据缺失、无效或会话过期。用户会话可按刷新流程处理 |
| `403` | 权限不足，例如访问其他 RAG 范围或远程管理未开放 |
| `404` | 路由或资源不存在。跨用户读取实例、会话和运行也返回此状态 |
| `409` | 会话已有活跃任务、幂等键输入不一致或其他资源冲突 |
| `413`、`415`、`422` | 请求过大、媒体类型不符或参数无效 |
| `426`、`429` | 需要 HTTPS，或请求、运行数量达到限额 |
| `502`、`503`、`504` | 上游不可用、服务未配置或调用超时 |

## 本地 STDIO

本地程序启动同一个管理器的 `local` 模式，使用 UTF-8 JSON Lines。每行发一个请求，等待该请求的终止帧再发下一行。它直接在进程里分派路由，上游 HTTP 服务仍须可用。路径和鉴权规则与 HTTP 一致。无需单独启动管理器的 HTTP 模式。

```powershell
'{"id":"health-1","method":"GET","path":"/health"}' | .\.venv\Scripts\python.exe -B main.py local
```

| 请求字段 | 用法 |
| --- | --- |
| `id`、`method`、`path` | 字符串请求 ID、HTTP 方法、以 `/` 开头的路径和可选查询参数 |
| `headers` | 字符串字典或 `[[name,value],…]`，例如 `{"Authorization":"Bearer <token>"}` |
| `json`、`body`、`body_base64` | 可选且最多选一项。分别为 JSON 值、UTF-8 字符串和 Base64 字节 |

每个响应帧携带同一个 `id`。`type:"start"` 包含 `status` 和 `headers`，后续 `type:"data"` 包含 `body_base64`，按顺序解码拼接，最后收到 `type:"end"`。本地封装错误或流中断以 `type:"error"` 终止，帧包含 `status` 和 `error`。需要并行调用或长期订阅 MCP 事件时使用 HTTP。两种入口的请求体默认上限为 8 MiB，RAG 导入的 JSON 上限为 32 MiB。
