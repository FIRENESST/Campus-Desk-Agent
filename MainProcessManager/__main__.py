"""The local developer superaccount and process console."""

import argparse
import getpass
import sqlite3
import subprocess
import sys
import warnings
from contextlib import closing
from pathlib import Path

from .cas import HELP as CAS_HELP, manage_cas
from .control import ROOT, ControlStore, control_lock, state_directory
from .control_pipe import ControlPipe
from .external import dispatch_external
from .online_admin import HELP as ADMIN_HELP, manage_online_admin
from .processes import Supervisor
from .runtime_admin import RuntimeAdmin
from .services import CORE, read_definition
from .teacher_credentials import HELP as CREDENTIALS_HELP, manage_credentials


HELP = """status [进程]           查看已登记进程、状态及 PID
start 进程|core|all    启动进程及其依赖
stop 进程|all          停止受管进程
restart 进程           重启受管进程
reload                 热重启应用进程并重试上次未恢复的进程
external help          管理校园助手与 CAS 的局域网和 FRP 入口
register 名称 JSON文件  登记一个额外进程
unregister 名称        移除一条自定义登记
logs 进程              显示日志文件路径
audit                  查看最近的本地操作
passwd                 修改超级账户密码
lock                   锁定控制台，服务继续运行
exit                   按逆序停止受管进程并退出
""" + CREDENTIALS_HELP + "\n" + CAS_HELP + ADMIN_HELP
COMMAND_ERRORS = (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired)


def _secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("请输入密码的本地终端不支持隐藏输入") from None


def _new_secret():
    password = _secret("新密码（15–128 字符） ")
    if password != _secret("再次输入新密码 "):
        raise ValueError("两次密码输入不一致")
    return password


def _login(store, *, locked=False):
    while True:
        try:
            username = input("开发超级账户 ").strip()
            return store.authenticate(username, _secret("密码 "))
        except ValueError as exc:
            print(exc, file=sys.stderr)
        except KeyboardInterrupt:
            if not locked:
                raise
            print("\n控制台已锁定")


def _start_group(supervisor, actor, names):
    for name in names:
        try:
            print(f"{name}  {supervisor.start(actor, name)}", flush=True)
        except COMMAND_ERRORS as exc:
            print(f"{name}  {exc}", file=sys.stderr)


def _dispatch(command, arg, actor, store, supervisor):
    if command in {"start", "stop", "restart", "reload", "register", "unregister", "external"}:
        with supervisor.command_lock:
            return _run_command(command, arg, actor, store, supervisor)
    return _run_command(command, arg, actor, store, supervisor)


def _run_command(command, arg, actor, store, supervisor):
    match command:
        case "help":
            print(HELP)
        case "status":
            for name, (status, pid) in supervisor.status(arg or None).items():
                print(f"{name:<22} {status:<8} {pid if pid else '-'}")
        case "start":
            if arg == "core":
                names = tuple(name for name in CORE if supervisor.services[name].start_all)
            elif arg == "all":
                names = [name for name, service in supervisor.services.items() if service.start_all]
            else:
                names = (arg,)
            _start_group(supervisor, actor, names)
        case "stop":
            if arg == "all":
                supervisor.stop_all(actor)
            else:
                supervisor.stop(actor, arg)
        case "restart":
            supervisor.stop(actor, arg)
            _start_group(supervisor, actor, (arg,))
        case "reload":
            if arg:
                raise ValueError("请直接输入 reload")
            print("正在热重启应用进程…", flush=True)
            results = supervisor.reload(actor)
            if not results:
                print("当前没有需要热重启或恢复的应用进程")
            for name, status in results.items():
                print(f"{name}  {status}", flush=True)
        case "external":
            dispatch_external(arg, actor, supervisor)
        case "register":
            name, _, filename = arg.partition(" ")
            if not filename.strip():
                raise ValueError("请使用 register 名称 JSON文件")
            definition = read_definition(Path(filename.strip().strip('"')))
            supervisor.register(actor, name, definition)
        case "unregister":
            supervisor.unregister(actor, arg)
        case "logs":
            print(supervisor.log_path(arg))
            if arg == "postgresql":
                print(supervisor.logs / "postgresql-server.log")
        case "audit":
            for record in store.recent_events():
                print("  ".join(record))
        case "credentials":
            manage_credentials(arg, actor, store, _secret)
        case "cas":
            manage_cas(arg, actor, store, _secret)
        case "admin":
            manage_online_admin(arg, actor, store, supervisor.root)
        case "passwd":
            store.change_password(actor, _secret("当前密码 "), _new_secret())
        case _:
            raise ValueError("未知命令，使用 help 查看用法")


def _console(store, supervisor, actor, runtime):
    print("已进入本地主管理器。输入 help 查看命令。")
    try:
        while True:
            try:
                line = input(f"{actor}> ").strip()
                command, _, arg = line.partition(" ")
                arg = arg.strip()
                if not command:
                    continue
                if command == "exit":
                    runtime.close()
                    supervisor.stop_all(actor)
                    return
                if command == "lock":
                    store.record(actor, "console.lock", "local")
                    actor = _login(store, locked=True)
                    continue
                _dispatch(command, arg, actor, store, supervisor)
            except COMMAND_ERRORS as exc:
                print(exc, file=sys.stderr)
            except KeyboardInterrupt:
                print()
    except EOFError:
        runtime.close()
        supervisor.stop_all(actor)


def main():
    parser = argparse.ArgumentParser(description="本机离线主管理器和开发超级账户")
    parser.add_argument("command", nargs="?", default="run", choices=("init", "run"))
    parser.add_argument("--state-dir", type=Path, help="本机私有状态目录")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("请在本地交互终端启动")
    try:
        directory = state_directory(args.state_dir)
        with control_lock(directory), closing(ControlStore(directory)) as store:
            if args.command == "init":
                if store.has_account():
                    raise ValueError("开发超级账户已初始化")
                name = input("账户名 [developer] ").strip() or "developer"
                store.initialize(name, _new_secret())
                print(f"已初始化本地开发超级账户 {name}")
                return
            if not store.has_account():
                raise ValueError("请先运行 py -m MainProcessManager init")
            actor = _login(store)
            with closing(Supervisor(store, ROOT)) as supervisor, closing(RuntimeAdmin(supervisor)) as runtime:
                with ControlPipe(supervisor.owns_interface_caller, runtime.handle) as pipe:
                    supervisor.control_pipe = pipe.address
                    _console(store, supervisor, actor, runtime)
    except COMMAND_ERRORS as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
