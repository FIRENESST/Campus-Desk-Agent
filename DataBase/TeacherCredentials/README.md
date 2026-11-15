# 教师电子凭据

`teacher-credentials` 是独立的本机服务，监听 `127.0.0.1:8192`。启动时按 `schema.sql` 创建 `teachers.sqlite3` 和安装密钥 `credential.key`。这里保存凭据编号、教师姓名、姓名与电话的带密钥摘要、电话尾号、有效期及签发审计。用户、邮箱验证、角色及校园 SSO 仍由 MainAgent 的 PostgreSQL 管理。

## 签发与核验

1. 在现有账号完成邮箱验证，再由授权管理员将账号设为教师。配置了校园 SSO 时，教师可先在网站的「设置」绑定校园账号。 [教师角色设置](../../server/MainAgent/main_agent/cloud/README.md)
2. 登录主管理器后执行 `start teacher-credentials`。
3. 执行 `credentials issue`，输入账号邮箱、姓名、电话号码及有效天数。默认 365 天。使用 `credentials verify` 查看有效期和关联状态，使用 `credentials update` 修改资料并生成新凭据，使用 `credentials revoke` 撤销。

签发和修改会校验有效教师账号与已验证邮箱。校园 SSO 尚未配置时，管理员可按已验证邮箱签发。若账号已绑定校园 SSO，凭据同时绑定该账号当前的 SSO 身份。邮箱、角色或 SSO 绑定发生变化后，需由管理员重新核验并更新凭据。

教师预约教室时由 MainAgent 按本人账号核验。学生预约时填写指导老师的姓名和电话，由服务核对有效凭据。MINTA 只收到预约项目和随机业务标识。凭据数据库和安装密钥应保存在本机私有工作区，备份时需要同时保留两者。
