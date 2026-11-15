"""Local CAS account administration after supervisor login."""

from server.CASserver.store import ADMIN_USERNAME, CASStore, STATE_SUBDIR, canonical_campus_number, canonical_username


HELP = """cas list                       列出本地校园认证账号
cas add 学工号                  创建本地校园认证账号
cas set-number 原账号 学工号    为现有账号指定登录学工号
cas passwd 学工号               重置密码并注销该账号的 CAS 会话
cas disable 学工号              停用本地校园认证账号
cas enable 学工号               启用本地校园认证账号
cas admin passwd               设置或更改 CAS 管理员 admin 的密码
"""


def manage_cas(arg, actor, control, secret):
    parts = arg.split()
    if parts in ([], ["help"]):
        print(HELP, end="")
        return
    if parts == ["list"]:
        for account in CASStore(control.directory / STATE_SUBDIR).accounts():
            state = "启用" if account["enabled"] else "停用"
            pending = "  待设置学工号" if not account["is_campus_number"] else ""
            print(f"{account['username']}  {state}{pending}")
        return
    if parts == ["admin", "passwd"]:
        password = _password(secret)
        CASStore(control.directory / STATE_SUBDIR).change_administrator_password(password)
        control.record(actor, "cas.admin.passwd", ADMIN_USERNAME)
        print("CAS 管理员密码已更新，请重新登录 CAS 管理台。")
        return
    if len(parts) == 3 and parts[0] == "set-number":
        number = CASStore(control.directory / STATE_SUBDIR).set_campus_number(parts[1], parts[2])
        control.record(actor, "cas.set-number", number)
        print(f"CAS 登录学工号已更新为 {number}。")
        return
    if len(parts) != 2 or parts[0] not in {"add", "passwd", "disable", "enable"}:
        raise ValueError("使用 cas help 查看用法")

    action, value = parts
    name = canonical_campus_number(value) if action == "add" else canonical_username(value)
    store = CASStore(control.directory / STATE_SUBDIR)
    if action in {"add", "passwd"}:
        operation = store.add if action == "add" else store.change_password
        operation(name, _password(secret))
    else:
        store.set_enabled(name, action == "enable")
    control.record(actor, f"cas.{action}", name)
    print(f"CAS 账号 {name} 已更新。")


def _password(secret):
    password = secret("CAS 新密码（8–128 位，含数字及小写字母） ")
    if password != secret("再次输入 CAS 新密码 "):
        raise ValueError("两次密码输入不一致")
    return password
