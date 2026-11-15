"""Manage routing feedback from an elevated shell on the server."""

import argparse
import ctypes
import json
import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from ..campus_cluster import AgentId, RoutingFeedback, build_campus_cluster
from ..campus_cluster.routing_feedback import DATA_DIR_ENV


def _is_local_admin() -> bool:
    if os.name == "nt":
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    return os.name == "posix" and os.geteuid() == 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="MainAgent 本机路由管理")
    parser.add_argument("--env-file", help="与 MainAgent 服务使用同一份环境配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="查看反馈学习开关")
    commands.add_parser("enable", help="开启样本采集和人工标签提示")
    commands.add_parser("disable", help="关闭样本采集和人工标签提示")
    commands.add_parser("introductions", help="查看角色自我介绍")
    roles = [role.value for role in AgentId]
    intro = commands.add_parser("set-introduction", help="从 UTF-8 文件更新角色自我介绍")
    intro.add_argument("role", choices=roles)
    intro.add_argument("file", type=Path)
    listing = commands.add_parser("list", help="查看脱敏样本和实际调用的角色")
    listing.add_argument("--limit", type=int, default=20)
    grade = commands.add_parser("grade", help="评估实际路由并指定正确的角色")
    grade.add_argument("sample_id")
    grade.add_argument("--score", type=int, choices=range(1, 6), required=True)
    expected = grade.add_mutually_exclusive_group(required=True)
    expected.add_argument("--targets", nargs="+", choices=roles)
    expected.add_argument("--clarify", action="store_true", help="正确行为是先向用户澄清")
    expected.add_argument("--direct", action="store_true", help="正确行为是由主智能体直接回答")
    delete = commands.add_parser("delete", help="删除一个样本及其评分")
    delete.add_argument("sample_id")
    args = parser.parse_args(argv)

    if not _is_local_admin():
        parser.error("请在服务器本机使用提升权限的管理员终端运行此命令")
    if args.env_file:
        load_dotenv(args.env_file, override=False, encoding="utf-8-sig")
    directory = os.getenv(DATA_DIR_ENV)
    if not directory:
        parser.error(f"请设置与服务相同的 {DATA_DIR_ENV}")

    try:
        store = RoutingFeedback(directory)
        if args.command == "status":
            output = {"enabled": store.is_enabled()}
        elif args.command in {"enable", "disable"}:
            enabled = args.command == "enable"
            store.set_enabled(enabled)
            output = {"enabled": enabled}
        elif args.command == "introductions":
            overrides = store.introductions()
            output = {
                profile.id.value: overrides.get(profile.id, profile.responsibility)
                for profile in build_campus_cluster().registry.profiles
            }
        elif args.command == "set-introduction":
            store.set_introduction(AgentId(args.role), args.file.read_text(encoding="utf-8-sig"))
            output = {"updated": args.role}
        elif args.command == "list":
            output = store.list_samples(args.limit)
        elif args.command == "grade":
            targets = tuple(AgentId(role) for role in (args.targets or ()))
            if not store.grade(args.sample_id, args.score, targets, direct=args.direct):
                parser.error("找不到该样本")
            output = {"graded": args.sample_id}
        else:
            if not store.delete_sample(args.sample_id):
                parser.error("找不到该样本")
            output = {"deleted": args.sample_id}
    except UnicodeError:
        parser.error("自我介绍文件必须使用 UTF-8 编码")
    except ValueError as exc:
        parser.error(str(exc))
    except (OSError, sqlite3.Error):
        parser.error("无法访问文件或路由数据库，请检查目录权限并确认默认 MainAgent 服务已初始化数据库")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
