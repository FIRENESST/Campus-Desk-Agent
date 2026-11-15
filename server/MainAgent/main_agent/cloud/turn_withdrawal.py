"""Preview and compensate a turn's recorded effects before hiding its messages."""

from uuid import uuid4

from ..campus_cluster.subskills.minta_campus import campus_intent
from .auth import audit
from .chat_effects import resolve_effect
from .errors import ServiceError, not_found
from .minta_clubs import club_effect
from .store import ACTIVE_RUNS, PostgresStore


class TurnWithdrawal:
    def __init__(self, operations):
        self.operations = operations
        self.db = operations.db

    @staticmethod
    def _turn(connection, who, run_id, *, lock=False):
        if lock:
            # The worker and retry use the same conversation -> run lock order.
            owner = connection.execute(
                """SELECT c.id FROM runs r JOIN conversations c ON c.id=r.conversation_id
                WHERE r.id=%s AND c.user_id=%s FOR UPDATE OF c""", (run_id, who.user_id),
            ).fetchone()
            if not owner:
                raise not_found()
        turn = connection.execute(
            """SELECT r.id,r.conversation_id,r.status,r.input,r.attempt,r.withdrawn_at,r.chat_effects_tracked,
               r.withdrawal_claim,r.withdrawal_expires_at>clock_timestamp() AS busy
               FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=%s AND c.user_id=%s"""
            + (" FOR UPDATE OF r" if lock else ""), (run_id, who.user_id),
        ).fetchone()
        if not turn:
            raise not_found()
        return turn

    @staticmethod
    def _effects(connection, who, run_id):
        return connection.execute(
            """SELECT e.id,e.kind,e.label,e.operation_id,e.state,o.service,o.state AS operation_state
               FROM chat_turn_effects e LEFT JOIN campus_minta_operations o
               ON o.id=e.operation_id AND o.user_id=%s WHERE e.run_id=%s
               ORDER BY e.created_at DESC,e.id DESC""", (who.user_id, run_id),
        ).fetchall()

    @staticmethod
    def _check_plan(turn, effects):
        if turn["busy"]:
            raise ServiceError(409, "withdrawal_busy", "这轮正在撤回，请稍后查看结果。")
        query = turn["input"].get("query", "")
        if not turn["chat_effects_tracked"] and not effects and (intent := campus_intent(query)) and intent.target:
            raise ServiceError(409, "withdrawal_untracked", "这轮旧校务指令缺少可回溯的操作记录，请到校园应用核对办理情况。")
        if any(item["state"] == "pending" for item in effects):
            raise ServiceError(409, "withdrawal_receipt_pending", "这轮有校务操作尚无法核实，请稍后重新检查或到校园应用查看。")
        return [item for item in effects if item["state"] == "applied"]

    async def preview(self, who, run_id):
        def read(connection):
            turn = self._turn(connection, who, run_id)
            if turn["withdrawn_at"]:
                raise not_found()
            PostgresStore.require_latest_turn(connection, turn["conversation_id"], run_id)
            return turn, self._effects(connection, who, run_id)

        turn, effects = await self.db.run(read)
        if not turn["busy"] and any(item["state"] == "pending" for item in effects):
            await self._reconcile(who, run_id, effects)
            turn, effects = await self.db.run(read)
        actions = self._check_plan(turn, effects)
        return {"attempt": turn["attempt"], "running": turn["status"] in ACTIVE_RUNS,
                "operations": [{"id": str(item["id"]), "label": item["label"]} for item in actions]}

    async def _reconcile(self, who, run_id, effects):
        pending = [item for item in effects if item["state"] == "pending"]
        for item in pending:
            if item["kind"] == "start" and item["operation_state"] in {"pending", "uncertain"}:
                await self.operations.reconcile_operation(who, item["operation_id"])
            elif item["kind"] == "club":
                try:
                    state = await club_effect(self.operations.gateway, who.user_id, item["id"])
                except ServiceError:
                    continue
                await self.db.run(lambda c: resolve_effect(c, item["id"], state))

        def reconcile_starts(connection):
            connection.execute(
                """UPDATE chat_turn_effects e SET state=CASE WHEN o.state='failed' THEN 'no_effect' ELSE 'applied' END
                FROM campus_minta_operations o WHERE e.run_id=%s AND e.state='pending' AND e.kind='start'
                AND o.id=e.operation_id AND o.user_id=%s AND o.state NOT IN ('pending','uncertain')""",
                (run_id, who.user_id),
            )
        await self.db.run(reconcile_starts)

    async def withdraw(self, who, run_id, attempt, approvals):
        claim = uuid4()

        def prepare(connection):
            # Related campus effects may be recorded in different conversations.
            connection.execute("SELECT id FROM users WHERE id=%s FOR NO KEY UPDATE", (who.user_id,))
            turn = self._turn(connection, who, run_id, lock=True)
            if turn["withdrawn_at"]:
                return None
            PostgresStore.require_latest_turn(connection, turn["conversation_id"], run_id)
            actions = self._check_plan(turn, self._effects(connection, who, run_id))
            if attempt != turn["attempt"] or approvals != [item["id"] for item in actions]:
                raise ServiceError(409, "withdrawal_plan_changed", "这轮的操作已变化，请重新确认撤回步骤。")
            if actions and connection.execute(
                """SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id
                   WHERE c.user_id=%s AND r.withdrawal_expires_at>clock_timestamp() LIMIT 1""", (who.user_id,),
            ).fetchone():
                raise ServiceError(409, "withdrawal_busy", "还有一轮校务正在撤回，请等它完成后再试。")
            connection.execute(
                """UPDATE runs SET withdrawal_started_at=COALESCE(withdrawal_started_at,now()),withdrawal_claim=%s,
                withdrawal_expires_at=clock_timestamp()+interval '2 minutes',
                finished_at=CASE WHEN status IN ('queued','running') THEN now() ELSE finished_at END,
                cancel_requested_at=CASE WHEN status IN ('queued','running') THEN now() ELSE cancel_requested_at END,
                result_status=CASE WHEN status IN ('queued','running') THEN 'uncertain' ELSE result_status END,
                status=CASE WHEN status IN ('queued','running') THEN 'cancelled' ELSE status END,lease_expires_at=NULL
                WHERE id=%s""", (claim, run_id),
            )
            connection.execute("DELETE FROM run_steps WHERE run_id=%s AND status='running'", (run_id,))
            if not actions:
                self._hide(connection, who, turn)
                return None
            return actions

        actions = await self.db.run(prepare)
        if actions is None:
            return
        try:
            for action in actions:
                await self.db.run(lambda c: self._claim_effect(c, who, run_id, claim))
                await self._undo(who, action)
                await self.db.run(lambda c: self._complete_effect(c, who, run_id, claim, action["id"]))
            await self.db.run(lambda c: self._finish(c, who, run_id, claim))
        except Exception:
            await self.db.run(lambda c: c.execute(
                "UPDATE runs SET withdrawal_claim=NULL,withdrawal_expires_at=NULL WHERE id=%s AND withdrawal_claim=%s",
                (run_id, claim),
            ))
            raise

    def _claim_effect(self, connection, who, run_id, claim):
        turn = self._turn(connection, who, run_id, lock=True)
        if turn["withdrawal_claim"] != claim or not turn["busy"]:
            raise ServiceError(409, "withdrawal_changed", "撤回状态已变化，请重新查看。")
        PostgresStore.require_latest_turn(connection, turn["conversation_id"], run_id)
        connection.execute("UPDATE runs SET withdrawal_expires_at=clock_timestamp()+interval '2 minutes' WHERE id=%s", (run_id,))
        return turn

    def _complete_effect(self, connection, who, run_id, claim, effect_id):
        self._claim_effect(connection, who, run_id, claim)
        connection.execute("UPDATE chat_turn_effects SET state='undone' WHERE id=%s AND run_id=%s", (effect_id, run_id))

    def _finish(self, connection, who, run_id, claim):
        turn = self._claim_effect(connection, who, run_id, claim)
        if connection.execute("SELECT 1 FROM chat_turn_effects WHERE run_id=%s AND state IN ('pending','applied')",
                              (run_id,)).fetchone():
            raise ServiceError(409, "withdrawal_incomplete", "还有校务操作待撤销，请重新查看。")
        self._hide(connection, who, turn)

    @staticmethod
    def _hide(connection, who, turn):
        connection.execute(
            "UPDATE runs SET withdrawn_at=now(),withdrawal_claim=NULL,withdrawal_expires_at=NULL WHERE id=%s", (turn["id"],),
        )
        connection.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (turn["conversation_id"],))
        audit(connection, who.user_id, "run.withdraw", "completed")

    async def _undo(self, who, action):
        if action["kind"] == "club":
            await club_effect(self.operations.gateway, who.user_id, action["id"], undo=True)
            return
        if not action["operation_id"]:
            raise ServiceError(409, "withdrawal_record_missing", "原校务记录已不可读取。")
        if action["kind"] == "close":
            result = await self.operations.restore_closed(who, action["operation_id"], action["id"])
            if result["state"] in {"active", "closed"}:
                return
        else:
            result = await self.operations.undo_started(who, action["operation_id"])
            if result["state"] == "closed":
                return
        raise ServiceError(409, "withdrawal_undo_unconfirmed", result["message"] or "暂时无法确认校务操作已撤销，请稍后重试。")
