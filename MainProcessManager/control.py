"""Local credentials, service registrations, and operator audit records."""

import hashlib
import hmac
import json
import msvcrt
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,39}\Z")
PASSWORD_PARAMETERS = {"n": 2**17, "r": 8, "p": 1, "dklen": 32, "maxmem": 256 * 1024**2}
SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    retry_after INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS registrations (
    name TEXT PRIMARY KEY,
    definition TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_services (
    name TEXT PRIMARY KEY,
    configuration TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL
);
"""


def local_path(path: Path) -> Path:
    if not path.is_absolute() or path.drive.startswith("\\\\"):
        raise ValueError("需要本机绝对路径")
    resolved = path.resolve()
    if resolved.drive.startswith("\\\\"):
        raise ValueError("需要本机绝对路径")
    return resolved


def state_directory(override: Path | None) -> Path:
    if override is not None:
        return local_path(override.absolute())
    appdata = os.getenv("LOCALAPPDATA", "")
    if not appdata:
        raise ValueError("无法找到当前用户的 LOCALAPPDATA，请指定私有的 --state-dir")
    workspace = hashlib.sha256(os.fsencode(os.path.normcase(str(ROOT)))).hexdigest()[:16]
    return local_path(Path(appdata) / "MainProcessManager" / workspace)


@contextmanager
def control_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "supervisor.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("此状态目录已有一个主管理器在运行") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _new_password(password: str):
    if not 15 <= len(password) <= 128 or not password.strip():
        raise ValueError("密码须为 15–128 字符")
    salt = secrets.token_bytes(16)
    return salt, _password_hash(password, salt)


def _password_hash(password: str, salt: bytes):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **PASSWORD_PARAMETERS)


class ControlStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "control.sqlite3"
        self.db = sqlite3.connect(self.path)
        try:
            self.db.executescript(SCHEMA)
        except BaseException:
            self.db.close()
            raise
        self.db.row_factory = sqlite3.Row

    def close(self):
        self.db.close()

    def has_account(self) -> bool:
        return self.db.execute("SELECT 1 FROM account").fetchone() is not None

    def initialize(self, name: str, password: str):
        if self.has_account():
            raise ValueError("开发超级账户已初始化")
        if not ACCOUNT_NAME.fullmatch(name):
            raise ValueError("账户名需为 3–40 个字母、数字、点、下划线或连字符，并以字母开头")
        salt, digest = _new_password(password)
        with self.db:
            self.db.execute("INSERT INTO account(id, name, salt, password_hash) VALUES (1, ?, ?, ?)", (name, salt, digest))
            self._record(name, "account.init", "local")

    def authenticate(self, name: str, password: str) -> str:
        account = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if account is None:
            raise ValueError("请先初始化开发超级账户")
        now = int(time.time())
        if account["retry_after"] > now:
            raise ValueError(f"请在 {account['retry_after'] - now} 秒后重试")
        candidate = _password_hash(password[:128], account["salt"])
        valid_password = hmac.compare_digest(candidate, account["password_hash"])
        if name == account["name"] and len(password) <= 128 and valid_password:
            with self.db:
                self.db.execute("UPDATE account SET failures=0, retry_after=0 WHERE id=1")
                self._record(account["name"], "login.ok", "local")
            return account["name"]
        failures = account["failures"] + 1
        delay = min(60, 2 ** min(failures - 1, 6)) if failures >= 3 else 0
        with self.db:
            self.db.execute("UPDATE account SET failures=?, retry_after=? WHERE id=1", (failures, now + delay))
            self._record("-", "login.failed", "local")
        raise ValueError("账户名或密码错误")

    def change_password(self, actor: str, old: str, new: str):
        self.authenticate(actor, old)
        salt, digest = _new_password(new)
        with self.db:
            self.db.execute("UPDATE account SET salt=?, password_hash=? WHERE id=1", (salt, digest))
            self._record(actor, "account.password", "local")

    def registrations(self):
        return {name: json.loads(data) for name, data in self.db.execute("SELECT name, definition FROM registrations")}

    def register(self, actor: str, name: str, definition: dict):
        with self.db:
            self.db.execute("INSERT INTO registrations(name, definition) VALUES (?, ?)",
                            (name, json.dumps(definition, ensure_ascii=False)))
            self._record(actor, "process.register", name)

    def unregister(self, actor: str, name: str):
        with self.db:
            self.db.execute("DELETE FROM registrations WHERE name=?", (name,))
            self._record(actor, "process.unregister", name)

    def external_config(self, name: str):
        row = self.db.execute("SELECT configuration FROM external_services WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def configure_external(self, actor: str, name: str, configuration: dict):
        with self.db:
            self.db.execute(
                """INSERT INTO external_services(name, configuration) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET configuration=excluded.configuration""",
                (name, json.dumps(configuration)),
            )
            self._record(actor, "external.configure", name)

    def _record(self, actor, action, target):
        self.db.execute("INSERT INTO audit(actor, action, target) VALUES (?, ?, ?)", (actor, action, target))

    def record(self, actor, action, target):
        with closing(sqlite3.connect(self.path, timeout=5)) as connection, connection:
            connection.execute("INSERT INTO audit(actor,action,target) VALUES (?,?,?)", (actor, action, target))

    def recent_events(self):
        return self.db.execute("SELECT time, actor, action, target FROM audit ORDER BY id DESC LIMIT 30").fetchall()
