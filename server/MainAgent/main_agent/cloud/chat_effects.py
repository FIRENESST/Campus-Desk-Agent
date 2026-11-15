"""Record a chat's external effects before dispatching the write."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import ServiceError

if TYPE_CHECKING:
    from .runner import ExecutionContext


@dataclass(frozen=True)
class ChatEffect:
    context: "ExecutionContext"
    label: str


def lock_effect_run(connection, who, effect):
    context = effect.context
    active = connection.execute(
        """SELECT r.id FROM runs r JOIN conversations c ON c.id=r.conversation_id
        JOIN auth_sessions s ON s.id=r.initiator_session_id AND s.user_id=c.user_id
        WHERE r.id=%s AND r.worker_id=%s AND c.user_id=%s AND s.id=%s
        AND s.revoked_at IS NULL AND s.expires_at>now() AND r.attempt=1
        AND r.status='running' AND r.lease_expires_at>now() AND r.cancel_requested_at IS NULL
        AND r.withdrawal_started_at IS NULL AND r.withdrawn_at IS NULL FOR UPDATE OF r""",
        (context.run_id, context.worker_id, who.user_id, who.session_id),
    ).fetchone()
    if not active:
        raise ServiceError(409, "turn_inactive", "这轮处理已结束，校务操作已停止。")


def add_effect(connection, effect, kind, operation_id=None):
    return connection.execute(
        "INSERT INTO chat_turn_effects(run_id,kind,label,operation_id) VALUES(%s,%s,%s,%s) RETURNING id",
        (effect.context.run_id, kind, effect.label[:300], operation_id),
    ).fetchone()["id"]


def resolve_effect(connection, effect_id, state):
    connection.execute("UPDATE chat_turn_effects SET state=%s WHERE id=%s AND state='pending'", (state, effect_id))
