"""Provision online administrators from the authenticated local console."""

import json
import subprocess
from pathlib import Path

from .online_admin_store import OnlineAdminStore, admin_user_id


HELP = """
admin grant 用户UUID      授予已认证邮箱账号在线管理权限
admin revoke 用户UUID     撤销在线管理权限及管理会话
admin list                查看已授权的邮箱和用户 UUID
admin verify-user 邮箱    人工认证普通用户的邮箱
"""


def _postgres(root, action, arguments):
    server = root / "server"
    agent = server / "MainAgent"
    result = subprocess.run(
        [str(agent / ".venv" / "Scripts" / "python.exe"), "-B", "-X", "utf8",
         str(server / "UnifiedInterfaceManagement" / "worker.py"), "postgres"],
        input=json.dumps({"action": action, "arguments": arguments},
                         ensure_ascii=False).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=agent,
        timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(result.stdout)
        if result.returncode:
            raise ValueError
        data = payload["data"]
        if payload["status"] == 200:
            return data
        error = data["error"]["message"]
    except (ValueError, TypeError, KeyError):
        raise ValueError("无法查询或更新普通用户，请检查 PostgreSQL 和 MainAgent 配置") from None
    raise ValueError(error)


def manage_online_admin(arg, actor, control, root: Path):
    parts = arg.split()
    if parts in ([], ["help"]):
        print(HELP, end="")
        return
    if parts == ["list"]:
        accounts = OnlineAdminStore(control.directory / "online-admin").accounts()
        for account in accounts:
            print(f"{account['user_id']}  {account['email']}")
        if not accounts:
            print("暂无已授权的在线管理员。")
        return
    if len(parts) != 2 or parts[0] not in {"grant", "revoke", "verify-user"}:
        raise ValueError("使用 admin help 查看用法")
    action, value = parts
    if action == "verify-user":
        user = _postgres(root, "verify_user", {"email": value, "actor": "local:" + actor})
        control.record(actor, "admin.verify-user", user["id"])
        print(f"用户 {user['id']}  {'已完成认证' if user['changed'] else '已认证'}")
        return
    user_id = admin_user_id(value)
    store = OnlineAdminStore(control.directory / "online-admin")
    if action == "grant":
        user = _postgres(root, "admin_identity", {"id": user_id})
        store.grant(actor, user_id, user["email"])
        message = f"已授予 {user['email']}（{user_id}）在线管理权限。"
    else:
        revoked = store.revoke(actor, user_id)
        message = f"已撤销 {user_id} 的在线管理权限和管理会话。" if revoked else f"{user_id} 尚未获授在线管理权限。"
    control.record(actor, f"admin.{action}", user_id)
    print(message)
