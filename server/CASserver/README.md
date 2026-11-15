# 自建 CAS 校园认证

CAS 是由主管理器启动的独立身份服务。学生使用学号，教职工使用工号，两类账号均使用 CAS 密码登录。通过内网或 FRP 访问时，从校园助手点击「统一身份认证」会跳到独立 CAS 地址，登录完成后回到发起登录的校园助手。MainAgent 通过 `127.0.0.1:8193` 校验一次性服务票据。

## 启动与绑定

在 `server/MainAgent/.env.cloud` 选择本地 CAS。保留原有数据库、邮箱等配置。

```dotenv
CLOUD_SSO_PROTOCOL=cas
CLOUD_CAS_LOCAL=true
CLOUD_CAS_VALIDATION_PATH=p3/serviceValidate
CLOUD_PUBLIC_URL=http://127.0.0.1:8082
CLOUD_DEVELOPMENT=true
```

以上地址用于本机开发。`CLOUD_SSO_STATE_SECRET` 需要独立生成并由 MainAgent 副本共享。校园身份和资料使用 PostgreSQL，部署前执行现有迁移命令，确认已应用 `009_campus_profiles.sql` 和 `010_campus_access_rules.sql`。

新机器可为 CAS 安装独立的 Python 环境。在项目根目录执行以下命令。

```powershell
py -m venv .\server\CASserver\.venv
.\server\CASserver\.venv\Scripts\python.exe -m pip install -r .\server\CASserver\requirements.txt
```

重新打开并登录主管理器，在控制台执行以下命令。创建账号时按提示输入两次密码。

```text
cas add <学工号>
start user-website
```

网站启动时会带起 `cas-server`。用已验证的邮箱账号登录后按页面提示绑定校园身份。绑定时输入当前邮箱密码，再到独立页面输入学工号和 CAS 密码。个人资料会自动带入 CAS 验证过的学工号，该字段为只读。完成个人资料登记后，两种入口都能进入同一个账号的工作台。

登录页和 CAS 管理台都链接到 `/cas/privacy`。该页面可以在登录前查看，说明学工号验证、会话票据和身份信息的处理方式。

## 管理与协议切换

在主管理器执行 `cas admin passwd` 为 `admin` 设置密码，再在部署主机上打开 [CAS 独立管理台](http://127.0.0.1:8193/admin)。新实例没有预设管理员密码，设置后才能登录管理台。再次运行该命令会更改密码并撤销已有的 CAS 管理会话。CAS 管理台提供学工号账号创建、批量导入、启停、学工号更换和密码重置。

创建账号时填写学工号、初始密码和确认密码。在账号列表可按学工号搜索，点「编辑」打开对应的操作表单。「CAS 运行日志」每 2 秒更新，显示本次进程的最近 120 条认证及管理事件。主管理器的 `logs cas-server` 可定位包含历次运行记录的日志文件。日志事件不包含账号提交值、密码或票据。

核对绑定邮箱、解除绑定及核验校园资料在校园助手 `/admin` 的「校园资料核验」中操作。管理员按校园名册核对后，可点击「一键核验本页」批量通过当前筛选页中学工号一致且账号有效的资料。提交时会重新检查资料版本和账号状态。

「表格批量创建」支持上传 Excel `.xlsx` 或 UTF-8 `.csv`。也可以先下载 CSV 模板。首行填写「学工号」和「密码」。学工号列也接受「学号」、「工号」、`campus_number` 和旧模板中的「账号」、`username`，密码列也接受 `password`。学工号为 2 到 32 位，首位为数字或英文字母，至少包含一个数字，其余字符可使用英文字母、数字、点、下划线和连字符。英文字母不区分大小写。Excel 读取第一张工作表，建议将两列设为文本，以便保留学工号的前导零和密码原文。每次最多创建 100 个账号，文件上限 256 KiB。

导入先校验账号、密码和重复记录，通过后整批写入。某行有误或账号已存在时会报告行号，整批写入会取消。

| 主管理器命令 | 作用 |
| --- | --- |
| `cas admin passwd` | 设置或修改 CAS 管理员密码，并撤销已有管理会话 |
| `cas list` | 查看本地 CAS 账号、启停状态和是否待分配学工号 |
| `cas set-number <原登录标识> <学工号>` | 为旧账号指定学工号，或更换登录学工号 |
| `cas passwd <学工号>` | 重设 CAS 密码，撤销该 CAS 账号的身份源会话和待校验票据 |
| `cas disable <学工号>` | 停用 CAS 登录并撤销该账号的身份源会话和待校验票据 |
| `cas enable <学工号>` | 恢复 CAS 登录 |
| `status cas-server`、`logs cas-server` | 查看运行状态和日志位置 |
| `reload` | 重启已运行的 CAS、MainAgent、网站及其受管依赖方 |

升级时会沿用 2 到 32 位的纯数字旧账号作为学工号。旧别名需要分配学工号后才能登录。分配或更换学工号会保留原密码和平台绑定，并撤销旧 CAS 会话和票据。原登记号码与当前学工号不一致时，用户需要重新确认资料。

CAS 凭据和票据保存在主管理器私有状态目录的 `cas/cas.sqlite3`。平台邮箱认证、角色、账号绑定、登记资料和会话保存在 PostgreSQL。MainAgent 通过本机受管接口检查绑定的 CAS 账号是否启用并读取学工号。停用期间，已有平台会话也无法访问业务接口。线上解除绑定同时移除该绑定的登记资料并撤销平台登录会话。

主管理器可同时配置内网与 FRP 的 CAS 浏览器入口。本机 8082 仍可用同源开发入口。两个公网域名分开时，退出校园助手会结束平台会话，在 CAS 域名打开 `/cas/logout` 可结束该域名的校园认证会话。 [局域网与 FRP 配置](../../MainProcessManager/README.md#对外服务管理)

OIDC、SAML 2.0 和外部 CAS 继续通过 MainAgent 的适配器配置选择。切换协议后，已有用户可用邮箱密码登录并重新绑定该协议的身份。 [协议配置](../MainAgent/main_agent/cloud/SSO.md)
