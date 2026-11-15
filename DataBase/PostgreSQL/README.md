# PostgreSQL 空库初始化

只包含表结构和版本迁移。请为 Campus Desk Agent 准备专用的 PostgreSQL 17+ 实例或独立集群。此处的示例监听本机 `55432`，不会使用默认端口上的已有实例。所有命令从本仓库的**新克隆**执行。

如果由 Windows 主管理器启动 PostgreSQL，将 `MPM_POSTGRES_BIN` 指向安装中包含 `pg_ctl.exe` 的 `bin` 目录；数据目录固定在当前仓库 `DataBase/PostgreSQL/data`。也可以用你管理的独立数据库实例，但需要自行启动应用进程，不依赖主管理器的内置 PostgreSQL 登记。

1. 选择新数据目录，运行 PostgreSQL 的 `initdb -D DataBase/PostgreSQL/data -U postgres -A scram-sha-256 -W`。在新生成的 `postgresql.conf` 设置 `listen_addresses = '127.0.0.1'` 和 `port = 55432`，再用 `pg_ctl -D DataBase/PostgreSQL/data -w start` 启动独立集群。
2. 用 `psql -h 127.0.0.1 -p 55432 -U postgres -d postgres -W` 连接新集群，执行如下初始化。用 `\password` 安全输入新密码。

   ```sql
   CREATE ROLE agent_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
   \password agent_app
   CREATE DATABASE cloud_agent;
   REVOKE CONNECT ON DATABASE cloud_agent FROM PUBLIC;
   GRANT CONNECT ON DATABASE cloud_agent TO agent_app;
   ```

3. 将 [应用配置模板](../../server/MainAgent/.env.cloud.example) 复制到本机 `server/MainAgent/.env.cloud`，填入运行角色的 DSN。迁移仅使用临时环境变量 `CLOUD_MIGRATION_DATABASE_URL`，指向这个**新库**的所有者账号。然后在 `server/MainAgent` 执行：

   ```powershell
   .\.venv\Scripts\python.exe -B -m main_agent.cloud migrate --env-file .env.cloud
   Remove-Item Env:CLOUD_MIGRATION_DATABASE_URL
   ```

`migrations/` 为 MainAgent 当前的完整版本迁移链。新建库使用迁移命令即可；`schema.sql` 保留早期四表结构用于兼容参考。实际 DSN、数据库数据目录、导入文件和备份均在版本控制之外。
