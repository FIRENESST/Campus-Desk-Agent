"""Manage teacher credentials from the authenticated local supervisor console."""

import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .control import ROOT


HELP = "credentials issue|update|verify|revoke  管理教师电子凭据"
BASE = "http://127.0.0.1:8192"


def manage_credentials(action, actor, store, secret_input):
    if action in {"", "help"}:
        print(HELP)
        return
    if action not in {"issue", "update", "verify", "revoke"}:
        raise ValueError(HELP)
    path = ROOT / "DataBase" / "TeacherCredentials" / "credential.key"
    try:
        with path.open("rb") as handle:
            key = handle.read(33)
    except OSError:
        raise ValueError("请先启动 teacher-credentials") from None
    if len(key) != 32:
        raise ValueError("教师凭据服务密钥格式无效")
    token = hmac.digest(key, b'["operator"]', "sha256").hex()
    payload = {"actor": actor, "email": input("教师账号邮箱 ").strip()}
    if action in {"issue", "update"}:
        payload["contact"] = {"name": input("老师姓名 ").strip(), "phone": secret_input("老师电话号码 ")}
        days = input("凭据有效天数 [365] ").strip()
        payload["days"] = int(days) if days else 365
    request = Request(f"{BASE}/manage/{'inspect' if action == 'verify' else action}",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      method="POST")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=8) as response:
            result = json.loads(response.read(8192))
    except HTTPError as exc:
        with exc:
            try:
                message = json.loads(exc.read(8192))["error"]["message"]
            except (ValueError, TypeError, KeyError):
                message = "教师凭据操作未完成"
        raise ValueError(message) from None
    except (URLError, TimeoutError, ValueError):
        raise ValueError("教师凭据服务暂不可用") from None
    store.record(actor, f"credentials.{action}", result["id"])
    status = "已撤销" if result["revoked"] else "有效" if result["valid"] else "待重新核验"
    print(f"凭据 {result['id']}  {status}  {result['name']}  电话尾号 {result['phone_tail']}")
    print(f"有效期至 {result['valid_until']}  校园 SSO {'已关联' if result['campus_linked'] else '待关联'}")
