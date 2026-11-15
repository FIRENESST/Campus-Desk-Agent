import argparse
import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from .config import DEFAULT_CLOUD_PORT
from .roles import AcademicTitle, UserRole


def main():
    parser = argparse.ArgumentParser(description="多用户云智能体服务")
    parser.add_argument("command", choices=["serve", "migrate", "worker", "set-user-role"])
    parser.add_argument("--env-file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_CLOUD_PORT)
    parser.add_argument("--email", help="set-user-role 要修改的账号邮箱")
    parser.add_argument("--role", choices=tuple(role.value for role in UserRole), help="set-user-role 要设置的账号身份")
    parser.add_argument("--title", type=str.strip, choices=tuple(title.value for title in AcademicTitle),
                        help="set-user-role 设置的高校教师职称")
    args = parser.parse_args()
    if args.command == "set-user-role" and (not args.email or not args.role):
        parser.error("set-user-role 需要 --email 和 --role")
    if args.command != "set-user-role" and any(value is not None for value in (args.email, args.role, args.title)):
        parser.error("--email、--role 和 --title 用于 set-user-role")
    if args.env_file:
        load_dotenv(args.env_file, override=False, encoding="utf-8-sig")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command in {"migrate", "set-user-role"}:
            dsn = os.getenv("CLOUD_MIGRATION_DATABASE_URL", "")
            if not dsn:
                raise ValueError("请单独设置 CLOUD_MIGRATION_DATABASE_URL（数据库所有者）")
            if args.command == "migrate":
                from .migrate import migrate
                print("已执行迁移：", migrate(dsn))
            else:
                from .auth import set_user_role
                set_user_role(dsn, args.email, args.role, args.title)
                print("已更新账号身份和职称")
        elif args.command == "serve":
            import uvicorn
            uvicorn.run("main_agent.cloud.api:app", host=args.host, port=args.port, access_log=False)
        else:
            from .api import create_app

            async def work():
                app = create_app()
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                win_stop = getattr(signal, "SIGBREAK", None)
                previous = None
                if win_stop is not None:
                    previous = signal.signal(win_stop, lambda *_: loop.call_soon_threadsafe(stop.set))
                try:
                    async with app.router.lifespan_context(app):
                        await stop.wait()
                finally:
                    if previous is not None:
                        signal.signal(win_stop, previous)

            asyncio.run(work())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Drivers may place DSNs/SMTP details in exception text; print only safe config errors.
        print(str(exc) if isinstance(exc, ValueError) else f"启动失败（{type(exc).__name__}），请检查配置和数据库状态。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
