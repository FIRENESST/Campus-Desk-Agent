# 邮箱密码与可选择的校园 SSO

邮箱密码和校园认证是两个入口，业务访问统一要求绑定校园身份并完成资料登记。校园入口支持 **OIDC、CAS 2/3、SAML 2.0**，通过 `CLOUD_SSO_PROTOCOL` 选择一个协议并重启服务。客户端通过 `GET /v1/auth/providers` 获取当前入口和支持的协议。

同一平台用户可以绑定校园身份，之后两种登录方式共用相同的用户 ID、实例、会话历史和权限。不同协议的身份按 `(protocol, issuer, subject)` 区分；OIDC 的 subject 来自签名 ID Token 的 `sub`，CAS 来自服务票据校验的 user，SAML 来自持久 NameID。显示名称和邮箱不会被当作校园身份主键。

## 选择协议

| `CLOUD_SSO_PROTOCOL` | 需要向学校获取 | 当前支持的登录方式 |
| --- | --- | --- |
| `oidc` | Issuer、客户端 ID、客户端密钥、回调白名单 | OIDC 授权码、S256 PKCE、RS256 ID Token |
| `cas` | 本地 CAS 由主管理器按学工号发放账号。外部 CAS 需要 HTTPS 基础地址、服务白名单和属性说明 | CAS 2 `serviceValidate` 或 CAS 3 `p3/serviceValidate` |
| `saml2` | IdP entity ID、SSO 地址、签名证书、属性说明 | SP 发起 Redirect 请求、POST 响应、持久 NameID |
| 空 | 无 | 邮箱验证和账号维护，校园业务等待配置身份源 |

项目可使用自建 CAS 身份源。OIDC、SAML 2.0 与外部 CAS 适配器需要分别配置对应身份源，沿用各协议原有的校验流程。

先在 `.env.cloud` 中填写：

```dotenv
CLOUD_SSO_PROTOCOL=oidc
CLOUD_SSO_STATE_SECRET=替换为独立生成的随机密钥
```

可通过下面的命令生成密钥，各 API 副本保持一致，不要与数据库密码或校园客户端密钥复用：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

外部身份源使用 `CLOUD_PUBLIC_URL` 生成回调地址。自建 CAS 的内网和 FRP 回调从主管理器登记的 HTTPS 网站源中选择，回调路径由 MainAgent 固定。部署时应使用 HTTPS，反向代理不得记录回调查询串和认证请求体。

## OIDC

```dotenv
CLOUD_SSO_PROTOCOL=oidc
CLOUD_OIDC_ISSUER=https://身份源示例域名/issuer
CLOUD_OIDC_CLIENT_ID=由学校分配
CLOUD_OIDC_CLIENT_SECRET=由学校分配
CLOUD_OIDC_TOKEN_AUTH_METHOD=client_secret_basic
```

学校注册的重定向地址为：

```text
{CLOUD_PUBLIC_URL}/v1/auth/sso/campus/callback
```

从固定 issuer 获取 discovery 与 JWKS，校验 issuer、受众、授权方、RS256 签名、有效期、iat、nonce，以及返回时存在的 at_hash/c_hash。只接受已声明支持授权码、S256 和 RS256 的身份源；密钥需要带 kid。客户端认证可选择 `client_secret_basic` 或 `client_secret_post`。

OIDC 在 OAuth 2.0 上提供身份认证。仅有 OAuth 2.0 授权接口、没有 OIDC ID Token 的服务不能直接使用这个适配器。参考 [OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html)、[Discovery](https://openid.net/specs/openid-connect-discovery-1_0.html) 和 [OAuth 安全规范](https://www.rfc-editor.org/rfc/rfc9700.html)。

## 项目自建 CAS

```dotenv
CLOUD_SSO_PROTOCOL=cas
CLOUD_CAS_LOCAL=true
CLOUD_CAS_VALIDATION_PATH=p3/serviceValidate
```

从内网或 FRP 的网站发起认证时，浏览器跳转到配对的 CAS HTTPS 地址。用户输入学工号和 CAS 密码后，CAS 将浏览器送回发起登录的网站。MainAgent 向本机 CAS 校验票据，并读取学工号用于个人资料登记。管理员按学工号创建 CAS 账号，用户通过已验证的邮箱账号显式绑定校园身份。 [主管理器命令与登录流程](../../../CASserver/README.md)

## 外部 CAS

```dotenv
CLOUD_SSO_PROTOCOL=cas
CLOUD_CAS_LOCAL=false
CLOUD_CAS_BASE_URL=https://身份源示例域名/cas
CLOUD_CAS_VALIDATION_PATH=p3/serviceValidate
CLOUD_CAS_EMAIL_ATTRIBUTE=mail
CLOUD_CAS_TRUST_EMAIL=false
```

通过固定 CAS HTTPS 地址校验 ST 服务票据，发送与授权时完全一致的 service URL。拒绝 PT 代理票据、失败响应、重复结构、DTD 与 XML 外部实体。绑定模式同时要求 renew，以重新验证校园凭据。

service URL 包含随机 state 查询参数；学校的服务白名单应允许固定回调路径及其 state 参数。不能在验证阶段删除查询参数或换一个 service 地址。CAS 2 可将校验路径改为 `serviceValidate`。

默认不把 `mail` 属性视为已验证邮箱，因此首次用户先完成邮箱注册，再显式绑定校园身份。只有校方明确保证该属性由其验证、由其控制且稳定可靠，才设置 `CLOUD_CAS_TRUST_EMAIL=true`，允许用该邮箱创建新的平台账户。参考 [Apereo CAS 协议](https://apereo.github.io/cas/development/protocol/CAS-Protocol-Specification.html)。

## SAML 2.0

```dotenv
CLOUD_SSO_PROTOCOL=saml2
CLOUD_PUBLIC_URL=https://云智能体示例域名
CLOUD_SAML_IDP_ENTITY_ID=学校提供的IdP标识
CLOUD_SAML_IDP_SSO_URL=https://身份源示例域名/sso
CLOUD_SAML_IDP_CERT_FILE=C:/安全目录/idp-signing.pem
CLOUD_SAML_EMAIL_ATTRIBUTE=mail
CLOUD_SAML_TRUST_EMAIL=false
```

SP metadata 地址为 `{CLOUD_PUBLIC_URL}/v1/auth/sso/campus/metadata`，ACS 为 `{CLOUD_PUBLIC_URL}/v1/auth/sso/campus/callback`，默认 SP entity ID 为 metadata 地址，也可通过 `CLOUD_SAML_SP_ENTITY_ID` 明确设置。

使用 python3-saml 严格模式和固定 IdP 签名证书，要求 Response 与 Assertion 都签名，使用 RSA/SHA-256 或更强的受支持算法；验证时间、受众、issuer、请求 ID 和精确的 Destination/Recipient。只接受 persistent NameID，拒绝无关联请求的 IdP 发起响应、重复 Assertion ID 和 XML 实体。当前处理通过 TLS 传输的未加密 Assertion；尚未接入 HTTP-Artifact 或 IdP 单点登出。

如学校要求 SP 对 AuthnRequest 签名，同时配置 `CLOUD_SAML_SP_CERT_FILE` 与 `CLOUD_SAML_SP_KEY_FILE` 两个 PEM 文件。私钥留在服务端安全文件中。

SAML 的跨站 POST 回调需要 `SameSite=None; Secure` 浏览器绑定 Cookie，因此开发联调也需要 HTTPS。参考 [SAML Toolkit 说明](https://github.com/SAML-Toolkits/python3-saml)。

## 统一客户端流程

1. 客户端生成 43–128 位随机 PKCE `code_verifier`，在当前登录流程内保存；计算 `base64url(SHA256(verifier))` 得到 `code_challenge`。
2. `POST /v1/auth/sso/campus/start`，提交 `{mode:"login",code_challenge}`。响应返回授权地址，同时设置短期 HttpOnly 浏览器绑定 Cookie。
3. 在同一浏览器进入授权地址，由选定的身份源完成认证。回调校验成功后返回 `{completion_code,expires_in:60,mode}`。平台账号解析和会话签发在下一步执行。
4. 客户端向 `POST /v1/auth/sso/campus/finish` 提交 `{completion_code,code_verifier}`。校验通过后，在一个事务内解析账户归属、创建登录会话并返回原有格式的访问/刷新令牌。
5. 使用访问令牌读取 `GET /v1/me`。`campus_status` 依次为 `link_required`、`profile_required` 或 `ready`。新绑定用户使用 `GET /v1/profile` 获取年级选项，`PUT /v1/profile` 登记资料，达到 `ready` 后访问业务接口。

MainAgent 提供 JSON 回调。普通用户网站在同源处理回调与完成码兑换，再重定向回首页。同一浏览器一次只维护一个进行中的 SSO 流程，重新开始会替换其绑定 Cookie。

OIDC 额外有服务器到身份源的 PKCE；三个协议均有客户端到本地 finish 接口的 PKCE。登录事务、单次完成码和断言重放记录存入 PostgreSQL，支持回调与开始请求落在不同 API 副本。数据库不保存上游访问令牌、原始 ID Token、原始 SAML 响应或明文 PKCE verifier。

## 已有账号的绑定与解绑

校园认证返回已验证邮箱，但这个邮箱已经存在时，finish 返回 `409 account_link_required`，不会自动合并账户。

已有用户先通过邮箱密码登录，再调用 start：

```json
{
  "mode": "link",
  "code_challenge": "客户端生成的43位challenge",
  "current_password": "用户当前密码"
}
```

该请求必须携带当前访问令牌。重新完成校园认证后，finish 再次检查原登录会话有效性，才将校园身份绑定到原用户；返回 `{linked:true}`。不能把其他用户已绑定的校园身份转移过来。

- `GET /v1/auth/identities`：列出当前用户绑定的校园身份。
- `DELETE /v1/auth/identities/{id}`，请求体 `{current_password}`：验证本地密码后解绑，并撤销当前用户所有校园登录会话，保留有效的邮箱密码登录会话。
- 只有校园登录、尚无本地密码的用户，可使用已验证邮箱完成原有密码重置流程建立本地密码，再解绑，避免移除唯一登录方式。

切换校园协议不删除用户或实例。由于不同协议的 subject 未必相同，不会自动迁移绑定关系；已有用户应先准备邮箱密码登录方式，再显式绑定新协议下的校园身份。

本服务的 logout/logout-all 负责撤销本地会话；学校浏览器会话仍由校方控制，当前未接入上游全局登出或后通道登出。

## 迁移与测试

重新运行原有迁移命令会新增执行 `002_campus_sso.sql`；不修改已执行的 `001_cloud.sql`。新增外部身份、SSO 事务、完成码及断言重放表，允许纯 SSO 用户的密码哈希为空；已有密码哈希保持原值。

集成测试在临时 PostgreSQL 中运行，OIDC 使用真实 RSA 签名的本地模拟响应，CAS 使用校验服务模拟器，SAML 使用双重 XML 签名的响应。覆盖签名伪造、时效、受众、nonce、请求关联、账号显式绑定、权限隔离和单次使用；测试不连接校园网络。

维护时清理过期 SSO 事务与完成码；断言重放记录的保留时间应覆盖校方允许的全部断言有效期。敏感配置、签名证书轮换与其他认证配置一同管理。
