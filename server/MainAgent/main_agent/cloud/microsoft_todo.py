"""Account-bound Microsoft To Do authorization and background synchronization."""

import asyncio
import json
import logging
import re
import secrets
from datetime import UTC, datetime
from html import escape
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from .calendar_sync import Subscriber, personal_calendar
from .errors import ServiceError
from .microsoft_graph import GraphError, item_id, marker, with_marker
from .oidc import challenge
from .roles import UserRole
from .security import digest


SYNC_INTERVAL = 300
LEASE_SECONDS = 480
RUN_SECONDS = 360
STABLE_MARKER = "v1"
TASK_MARKER = re.compile(r"v1:task:([0-9a-f]{64}):([0-9a-f]{64})")


class AuthorizationFinish(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    state: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    code: str = Field(min_length=1, max_length=4096)


def _task_payload(event):
    start, end = (value.astimezone(CAMPUS_TIME_ZONE) for value in (event.starts_at, event.ends_at))
    detail = f"{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（北京时间）"
    location = f"<br>地点 {escape(event.location)}" if event.location else ""

    def graph_time(value):
        return {"dateTime": value.replace(tzinfo=None).isoformat(timespec="seconds"), "timeZone": "China Standard Time"}

    return {
        "title": f"[{event.category}] {event.title}"[:255],
        "body": {"contentType": "html", "content": f"<p>{detail}{location}</p>"},
        "startDateTime": graph_time(start), "dueDateTime": graph_time(end),
    }


def _hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


class MicrosoftTodo:
    def __init__(self, db, profiles, operations, access, settings, graph):
        self.db, self.profiles, self.operations, self.access = db, profiles, operations, access
        self.settings, self.graph = settings, graph
        try:
            self.cipher = Fernet(settings.todo_token_key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise ValueError("CLOUD_TODO_TOKEN_KEY 须为有效的 Fernet 密钥") from None

    async def status(self, user_id):
        row = await self.db.run(lambda c: c.execute(
            """SELECT account_name, connected_at, last_synced_at, sync_error, claimed_until > now() AS running
                 FROM microsoft_todo_connections WHERE user_id=%s""", (user_id,),
        ).fetchone())
        return {"configured": True, "connection": row}

    async def start(self, who):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

        def save(c):
            c.execute("DELETE FROM microsoft_todo_authorizations WHERE expires_at < now()")
            c.execute(
                """INSERT INTO microsoft_todo_authorizations (user_id,session_id,state_hash,verifier,expires_at)
                   VALUES (%s,%s,%s,%s,now()+interval '10 minutes')
                   ON CONFLICT (user_id) DO UPDATE SET session_id=excluded.session_id,state_hash=excluded.state_hash,
                       verifier=excluded.verifier,expires_at=excluded.expires_at,consumed_at=NULL""",
                (who.user_id, who.session_id, digest(state), verifier),
            )

        await self.db.run(save)
        return {"authorization_url": self.graph.authorization_url(state, challenge(verifier))}

    async def finish(self, who, state, code):
        authorization = (who.user_id, who.session_id, digest(state))
        flow = await self.db.run(lambda c: c.execute(
            """UPDATE microsoft_todo_authorizations SET consumed_at=now()
               WHERE user_id=%s AND session_id=%s AND state_hash=%s
               AND expires_at>now() AND consumed_at IS NULL RETURNING verifier""", authorization,
        ).fetchone())
        if not flow:
            raise ServiceError(409, "todo_authorization_expired", "授权已过期，请从日历同步页面重新发起")
        try:
            tokens = await self.graph.redeem(code, flow["verifier"])
            identity = await self.graph.identity(tokens.access)
            account_id = item_id(identity)
        except GraphError:
            raise ServiceError(502, "todo_authorization_failed", "Microsoft To Do 授权未完成，请重新尝试") from None
        account_name = identity.get("displayName")
        account_name = account_name[:200] if isinstance(account_name, str) and account_name.strip() else "Microsoft 账号"
        refresh, access, valid_for = self._encrypted_tokens(tokens)

        def save(c):
            accepted = c.execute(
                """DELETE FROM microsoft_todo_authorizations WHERE user_id=%s AND session_id=%s
                   AND state_hash=%s AND expires_at>now() AND consumed_at IS NOT NULL RETURNING user_id""",
                authorization,
            ).fetchone()
            if not accepted:
                raise ServiceError(409, "todo_authorization_expired", "授权已取消，请重新发起")
            active = c.execute(
                """SELECT 1 FROM auth_sessions WHERE id=%s AND user_id=%s
                   AND revoked_at IS NULL AND expires_at>now() FOR UPDATE""",
                (who.session_id, who.user_id),
            ).fetchone()
            if not active:
                raise ServiceError(409, "todo_authorization_expired", "授权会话已过期")
            row = c.execute(
                """INSERT INTO microsoft_todo_connections
                   (user_id,microsoft_id,account_name,refresh_token,access_token,access_expires_at)
                   VALUES (%s,%s,%s,%s,%s,now()+%s*interval '1 second')
                   ON CONFLICT (user_id) DO UPDATE SET microsoft_id=excluded.microsoft_id,account_name=excluded.account_name,
                      refresh_token=excluded.refresh_token,access_token=excluded.access_token,
                      access_expires_at=excluded.access_expires_at, next_sync_at=now(), sync_error=NULL,
                      list_id=CASE WHEN microsoft_todo_connections.microsoft_id=excluded.microsoft_id THEN microsoft_todo_connections.list_id END,
                      calendar_hash=NULL,last_synced_at=NULL,claim_id=NULL,claimed_until=NULL
                   WHERE microsoft_todo_connections.claim_id IS NULL OR microsoft_todo_connections.claimed_until<=now()
                   RETURNING user_id""",
                (who.user_id, account_id, account_name, refresh, access, valid_for),
            ).fetchone()
            if row is None:
                raise ServiceError(409, "todo_busy", "上一轮同步仍在运行，请稍后重新授权")

        try:
            await self.db.run(save)
        except ServiceError as exc:
            if exc.code == "conflict":
                raise ServiceError(409, "todo_account_already_linked", "该 Microsoft 账号已连接另一个校园账号") from None
            raise

    async def disconnect(self, who):
        def remove(c):
            c.execute("DELETE FROM microsoft_todo_authorizations WHERE user_id=%s", (who.user_id,))
            c.execute("DELETE FROM microsoft_todo_connections WHERE user_id=%s", (who.user_id,))

        await self.db.run(remove)

    async def queue(self, who):
        row = await self.db.run(lambda c: c.execute(
            """UPDATE microsoft_todo_connections
               SET next_sync_at=CASE WHEN sync_error='microsoft_throttled' OR claimed_until>now() THEN next_sync_at ELSE now() END,
                   calendar_hash=CASE WHEN claimed_until>now() THEN calendar_hash END
               WHERE user_id=%s AND sync_error IS DISTINCT FROM 'reauthorize' RETURNING user_id""",
            (who.user_id,),
        ).fetchone())
        if not row:
            raise ServiceError(409, "todo_connection_required", "请先连接或重新授权 Microsoft To Do")

    async def run(self):
        while True:
            try:
                worked = await self.run_one()
            except Exception as exc:
                logging.getLogger(__name__).warning("Microsoft To Do job failed (%s)", type(exc).__name__)
                worked = False
            if not worked:
                await asyncio.sleep(3)

    async def run_one(self):
        claim = uuid4()
        row = await self.db.run(lambda c: c.execute(
            """WITH next AS (
                SELECT t.user_id FROM microsoft_todo_connections t JOIN users u ON u.id=t.user_id
                WHERE u.is_active AND (%s=FALSE OR u.verified_at IS NOT NULL)
                    AND t.next_sync_at<=now() AND (t.claimed_until IS NULL OR t.claimed_until<=now())
                    AND t.sync_error IS DISTINCT FROM 'reauthorize'
                ORDER BY t.next_sync_at,t.user_id LIMIT 1 FOR UPDATE OF t SKIP LOCKED
               )
               UPDATE microsoft_todo_connections t SET claim_id=%s,claimed_until=now()+%s*interval '1 second'
               FROM next WHERE t.user_id=next.user_id RETURNING t.*""",
            (self.settings.require_verified_email, claim, LEASE_SECONDS),
        ).fetchone())
        if not row:
            return False
        try:
            async with asyncio.timeout(RUN_SECONDS):
                account = await self.db.run(lambda c: c.execute(
                    "SELECT role FROM users WHERE id=%s AND is_active", (row["user_id"],),
                ).fetchone())
                if account is None:
                    raise ServiceError(401, "invalid_credentials", "账号未启用")
                who = Subscriber(row["user_id"], UserRole(account["role"]))
                await self.profiles.require_ready(who)
                await self._synchronize(who, row)
        except asyncio.CancelledError:
            await self._complete(row, "microsoft_unavailable", 0)
            raise
        except InvalidToken:
            await self._complete(row, "reauthorize", SYNC_INTERVAL)
        except ServiceError as exc:
            identity = {"campus_link_required", "campus_profile_required", "invalid_credentials"}
            await self._complete(row, "campus_identity" if exc.code in identity else "schedule_unavailable", 60)
        except GraphError as exc:
            if exc.code == "access_expired":
                await self.db.run(lambda c: c.execute(
                    """UPDATE microsoft_todo_connections SET access_expires_at=now()
                       WHERE user_id=%s AND claim_id=%s""", (row["user_id"], row["claim_id"]),
                ))
                await self._complete(row, "microsoft_unavailable", 30)
            else:
                await self._complete(row, exc.code, exc.retry_after)
        except TimeoutError:
            await self._complete(row, "microsoft_unavailable", 60)
        else:
            await self._complete(row, None, SYNC_INTERVAL)
        return True

    def _encrypted_tokens(self, tokens):
        return self.cipher.encrypt(tokens.refresh.encode()), self.cipher.encrypt(tokens.access.encode()), tokens.expires_in

    async def _running(self, row):
        current = await self.db.run(lambda c: c.execute(
            """SELECT 1 FROM microsoft_todo_connections t JOIN users u ON u.id=t.user_id
               WHERE t.user_id=%s AND t.claim_id=%s AND t.claimed_until>now() AND u.is_active
                   AND (%s=FALSE OR u.verified_at IS NOT NULL)""",
            (row["user_id"], row["claim_id"], self.settings.require_verified_email),
        ).fetchone())
        if not current:
            raise GraphError()

    async def _access_token(self, row):
        if (row["access_expires_at"] - datetime.now(UTC)).total_seconds() > 120:
            return self.cipher.decrypt(row["access_token"]).decode()
        await self._running(row)
        tokens = await self.graph.refresh(self.cipher.decrypt(row["refresh_token"]).decode())
        refresh, access, valid_for = self._encrypted_tokens(tokens)
        await self.db.run(lambda c: c.execute(
            """UPDATE microsoft_todo_connections SET refresh_token=%s,access_token=%s,
               access_expires_at=now()+%s*interval '1 second' WHERE user_id=%s AND claim_id=%s""",
            (refresh, access, valid_for, row["user_id"], row["claim_id"]),
        ))
        return tokens.access

    async def _complete(self, row, error, delay):
        await self.db.run(lambda c: c.execute(
            """UPDATE microsoft_todo_connections SET claim_id=NULL,claimed_until=NULL,sync_error=%s,
                  next_sync_at=now()+%s*interval '1 second',
                  last_synced_at=CASE WHEN %s::text IS NULL THEN now() ELSE last_synced_at END
               WHERE user_id=%s AND claim_id=%s""",
            (error, delay, error, row["user_id"], row["claim_id"]),
        ))

    async def _list(self, row, access):
        expected = f"{STABLE_MARKER}:list:{digest(str(row['user_id']))}"

        def owns(item):
            return marker(item) == expected and item.get("isShared") is not True and item.get("isOwner") is not False

        if row["list_id"]:
            item = await self.graph.get_list(access, row["list_id"])
            if item and marker(item) == expected and not owns(item):
                raise GraphError("shared_list", SYNC_INTERVAL)
            if item and owns(item):
                return item_id(item)
        item = None
        async for candidate in self.graph.lists(access):
            if marker(candidate) == expected and not owns(candidate):
                raise GraphError("shared_list", SYNC_INTERVAL)
            if owns(candidate):
                item = candidate
                break
        if item is None:
            await self._running(row)
            item = await self.graph.create_list(access, expected)
        identifier = item_id(item)
        await self.db.run(lambda c: c.execute(
            "UPDATE microsoft_todo_connections SET list_id=%s WHERE user_id=%s AND claim_id=%s",
            (identifier, row["user_id"], row["claim_id"]),
        ))
        return identifier

    async def _synchronize(self, who, row):
        events = await personal_calendar(who, self.operations, self.access)
        desired = {}
        for event in events:
            payload = _task_payload(event)
            uid = event.uid.split("@", 1)[0]
            desired[uid] = (payload, _hash(payload))
        version = _hash({uid: signature for uid, (_, signature) in desired.items()})
        if version == row["calendar_hash"] and row["sync_error"] is None:
            return
        access = await self._access_token(row)
        list_id = await self._list(row, access)
        existing = {}
        for_removal = []
        async for task in self.graph.tasks(access, list_id):
            matched = TASK_MARKER.fullmatch(marker(task))
            if not matched:
                continue
            uid, signature = matched.groups()
            identifier = item_id(task)
            if uid in desired and uid not in existing:
                existing[uid] = identifier, signature
            else:
                for_removal.append(identifier)
        for uid, (payload, signature) in desired.items():
            current = existing.get(uid)
            if current and current[1] == signature:
                continue
            data = with_marker(payload, f"{STABLE_MARKER}:task:{uid}:{signature}")
            await self._running(row)
            if current:
                if not await self.graph.apply_task(access, list_id, "PATCH", current[0], data):
                    await self._running(row)
                    await self.graph.apply_task(access, list_id, "POST", None, data)
            else:
                await self.graph.apply_task(access, list_id, "POST", None, data)
        for identifier in for_removal:
            await self._running(row)
            await self.graph.apply_task(access, list_id, "DELETE", identifier, None)
        await self.db.run(lambda c: c.execute(
            "UPDATE microsoft_todo_connections SET calendar_hash=%s WHERE user_id=%s AND claim_id=%s",
            (version, row["user_id"], row["claim_id"]),
        ))

    async def close(self, worker):
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
