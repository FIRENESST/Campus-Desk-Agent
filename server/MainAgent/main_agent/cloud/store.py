import hashlib
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from .auth import audit
from .errors import ServiceError, not_found


RUN_FIELDS = ("r.id,r.conversation_id,r.status,r.result_status,r.error_message,r.created_at,r.started_at,"
              "r.finished_at,r.cancel_requested_at,r.attempt,r.withdrawn_at,r.withdrawal_started_at")
MESSAGE_FIELDS = ("m.id,m.conversation_id,m.run_id,m.role,m.content,m.created_at,r.status AS run_status,"
                  "r.attempt AS run_attempt,r.withdrawal_started_at")
ACTIVE_RUNS = {"queued", "running"}


class PostgresStore:
    def __init__(self, database, settings):
        self.db, self.settings = database, settings

    @staticmethod
    def _instance(connection, user_id, instance_id, lock=False):
        row = connection.execute(
            "SELECT * FROM agent_instances WHERE id=%s AND user_id=%s" + (" FOR NO KEY UPDATE" if lock else ""),
            (instance_id, user_id),
        ).fetchone()
        if not row:
            raise not_found()
        return row

    @staticmethod
    def _conversation(connection, user_id, conversation_id, lock=False):
        row = connection.execute(
            "SELECT * FROM conversations WHERE id=%s AND user_id=%s" + (" FOR UPDATE" if lock else ""),
            (conversation_id, user_id),
        ).fetchone()
        if not row:
            raise not_found()
        return row

    @staticmethod
    def _lock_user(connection, user_id):
        connection.execute("SELECT id FROM users WHERE id=%s FOR NO KEY UPDATE", (user_id,))

    async def create_instance(self, user_id, name):
        def create(connection):
            self._lock_user(connection, user_id)
            count = connection.execute("SELECT count(*) AS n FROM agent_instances WHERE user_id=%s", (user_id,)).fetchone()["n"]
            if count >= self.settings.max_instances:
                raise ServiceError(409, "instance_limit", "已达到实例数量上限")
            row = connection.execute(
                "INSERT INTO agent_instances(user_id,name) VALUES (%s,%s) RETURNING *", (user_id, name),
            ).fetchone()
            audit(connection, user_id, "instance.create", "completed")
            return row

        return await self.db.run(create)

    async def instances(self, user_id):
        return await self.db.run(lambda c: c.execute(
            "SELECT * FROM agent_instances WHERE user_id=%s ORDER BY is_default DESC,created_at DESC,id DESC", (user_id,),
        ).fetchall())

    async def instance(self, user_id, instance_id):
        return await self.db.run(lambda c: self._instance(c, user_id, instance_id))

    async def update_instance(self, user_id, instance_id, name=None, status=None, *, make_default=False):
        def update(connection):
            if make_default:
                self._lock_user(connection, user_id)
            self._instance(connection, user_id, instance_id, lock=True)
            if make_default:
                connection.execute(
                    "UPDATE agent_instances SET is_default=false,updated_at=now() WHERE user_id=%s AND is_default AND id<>%s",
                    (user_id, instance_id),
                )
            row = connection.execute(
                """UPDATE agent_instances SET name=COALESCE(%s,name),status=COALESCE(%s,status),
                is_default=is_default OR %s,updated_at=now() WHERE id=%s AND user_id=%s RETURNING *""",
                (name, status, make_default, instance_id, user_id),
            ).fetchone()
            audit(connection, user_id, "instance.update", "completed")
            return row

        return await self.db.run(update)

    async def delete_instance(self, user_id, instance_id):
        def delete(connection):
            self._lock_user(connection, user_id)
            instance = self._instance(connection, user_id, instance_id, lock=True)
            # The worker needs a SHARE lock on this instance before it can claim queued work.
            if connection.execute(
                """SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id
                WHERE c.instance_id=%s AND c.user_id=%s
                AND (r.status='running' OR r.withdrawal_expires_at>clock_timestamp()) LIMIT 1""",
                (instance_id, user_id),
            ).fetchone():
                raise ServiceError(409, "run_in_progress", "实例内仍有任务在执行或撤回，请在对应会话查看进度后重试")
            connection.execute("DELETE FROM agent_instances WHERE id=%s AND user_id=%s", (instance_id, user_id))
            if instance["is_default"]:
                replacement = connection.execute(
                    """SELECT id FROM agent_instances WHERE user_id=%s
                    ORDER BY (status='active') DESC,created_at DESC,id DESC LIMIT 1 FOR NO KEY UPDATE""",
                    (user_id,),
                ).fetchone()
                if replacement:
                    connection.execute(
                        "UPDATE agent_instances SET is_default=true,updated_at=now() WHERE id=%s",
                        (replacement["id"],),
                    )
                else:
                    connection.execute(
                        "INSERT INTO agent_instances(user_id,name,is_default) VALUES (%s,'默认实例',true)", (user_id,),
                    )
            audit(connection, user_id, "instance.delete", "completed")

        await self.db.run(delete)

    async def create_conversation(self, user_id, instance_id, title):
        def create(connection):
            self._instance(connection, user_id, instance_id, lock=True)
            return connection.execute(
                "INSERT INTO conversations(user_id,instance_id,title) VALUES (%s,%s,%s) RETURNING *",
                (user_id, instance_id, title),
            ).fetchone()

        return await self.db.run(create)

    async def update_conversation(self, user_id, conversation_id, title):
        def update(connection):
            row = connection.execute(
                "UPDATE conversations SET title=%s,updated_at=now() WHERE id=%s AND user_id=%s RETURNING *",
                (title, conversation_id, user_id),
            ).fetchone()
            if not row:
                raise not_found()
            audit(connection, user_id, "conversation.update", "completed")
            return row

        return await self.db.run(update)

    async def delete_conversation(self, user_id, conversation_id):
        def delete(connection):
            conversation = self._conversation(connection, user_id, conversation_id)
            self._instance(connection, user_id, conversation["instance_id"], lock=True)
            self._conversation(connection, user_id, conversation_id, lock=True)
            if connection.execute(
                """SELECT 1 FROM runs WHERE conversation_id=%s
                   AND (status='running' OR withdrawal_expires_at>clock_timestamp()) LIMIT 1""", (conversation_id,),
            ).fetchone():
                raise ServiceError(409, "run_in_progress", "会话仍有任务在执行或撤回，请查看进度后重试")
            connection.execute("DELETE FROM conversations WHERE id=%s AND user_id=%s", (conversation_id, user_id))
            audit(connection, user_id, "conversation.delete", "completed")

        await self.db.run(delete)

    async def conversations(self, user_id, instance_id, limit, before=None, *, search=""):
        def query(connection):
            self._instance(connection, user_id, instance_id)
            args = [user_id, instance_id]
            clause = ""
            if before:
                anchor = connection.execute(
                    "SELECT created_at,id FROM conversations WHERE id=%s AND user_id=%s AND instance_id=%s",
                    (before, user_id, instance_id),
                ).fetchone()
                if not anchor:
                    raise not_found()
                clause = " AND (created_at,id)<(%s,%s)"
                args.extend((anchor["created_at"], anchor["id"]))
            if search:
                clause += " AND strpos(lower(title),lower(%s))>0"
                args.append(search)
            args.append(limit + 1)
            rows = connection.execute(
                """SELECT c.*, (SELECT r.id FROM runs r WHERE r.conversation_id=c.id
                   AND r.status IN ('queued','running')) AS active_run_id
                   FROM conversations c WHERE user_id=%s AND instance_id=%s""" + clause
                + " ORDER BY created_at DESC,id DESC LIMIT %s", args,
            ).fetchall()
            return {"items": rows[:limit], "next_cursor": str(rows[limit-1]["id"]) if len(rows) > limit else None}

        return await self.db.run(query)

    async def messages(self, user_id, conversation_id, limit, before=None):
        def query(connection):
            self._conversation(connection, user_id, conversation_id)
            rows = connection.execute(
                "SELECT " + MESSAGE_FIELDS + """ FROM messages m LEFT JOIN runs r ON r.id=m.run_id
                WHERE m.conversation_id=%s AND m.role IN ('user','assistant') AND r.withdrawn_at IS NULL
                AND (%s::bigint IS NULL OR m.id<%s)
                ORDER BY m.id DESC LIMIT %s""",
                (conversation_id, before, before, limit + 1),
            ).fetchall()
            return {"items": list(reversed(rows[:limit])), "next_cursor": str(rows[limit-1]["id"]) if len(rows) > limit else None}

        return await self.db.run(query)

    @staticmethod
    def require_latest_turn(connection, conversation_id, run_id):
        latest = connection.execute(
            """SELECT m.run_id FROM messages m LEFT JOIN runs r ON r.id=m.run_id
               WHERE m.conversation_id=%s AND m.role IN ('user','assistant') AND r.withdrawn_at IS NULL
               ORDER BY m.id DESC LIMIT 1""", (conversation_id,),
        ).fetchone()
        if not latest or latest["run_id"] != run_id:
            raise ServiceError(409, "turn_not_latest", "只能重来或撤回当前会话的最新一轮。")

    async def begin_turn(self, user_id, conversation_id, query, targets, request_key, *, session_id=None):
        payload = {"query": query, "targets": targets}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        def create(connection):
            # Serialize quota accounting across this user's conversations and API replicas.
            self._lock_user(connection, user_id)
            conversation = self._conversation(connection, user_id, conversation_id)
            instance = self._instance(connection, user_id, conversation["instance_id"], lock=True)
            self._conversation(connection, user_id, conversation_id, lock=True)
            existing = connection.execute(
                "SELECT " + RUN_FIELDS + ",r.input_hash FROM runs r WHERE r.conversation_id=%s AND r.request_key=%s",
                (conversation_id, request_key),
            ).fetchone()
            if existing:
                if existing.pop("input_hash") != fingerprint:
                    raise ServiceError(409, "idempotency_mismatch", "同一幂等键不能用于不同请求")
                return existing
            if connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id=%s AND withdrawal_expires_at>clock_timestamp() LIMIT 1",
                (conversation_id,),
            ).fetchone():
                raise ServiceError(409, "withdrawal_busy", "这轮正在撤回，请等它完成后再发送。")
            self._require_turn_capacity(connection, user_id, instance)
            row = connection.execute(
                """INSERT INTO runs AS r(conversation_id,request_key,input_hash,input,initiator_session_id,chat_effects_tracked)
                VALUES (%s,%s,%s,%s,%s,TRUE) RETURNING """ + RUN_FIELDS,
                (conversation_id, request_key, fingerprint, Jsonb(payload), session_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO messages(conversation_id,run_id,role,content) VALUES (%s,%s,'user',%s)",
                (conversation_id, row["id"], query),
            )
            connection.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
            audit(connection, user_id, "run.submit", "queued")
            return row

        return await self.db.run(create)

    def _require_turn_capacity(self, connection, user_id, instance):
        if instance["status"] != "active":
            raise ServiceError(409, "instance_paused", "实例已暂停")
        active = connection.execute(
            """SELECT count(*) AS n FROM runs r JOIN conversations c ON c.id=r.conversation_id
            WHERE c.user_id=%s AND r.status IN ('queued','running')""", (user_id,),
        ).fetchone()["n"]
        limit = self.settings.max_active_runs_per_user
        if active >= limit:
            raise ServiceError(429, "run_limit", f"你最多可以同时处理 {limit} 个会话，请等一个结束后再试。")

    async def retry_turn(self, user_id, run_id, expected_attempt, key, *, session_id):
        def retry(connection):
            self._lock_user(connection, user_id)
            owner = connection.execute(
                """SELECT c.id,c.instance_id FROM runs r JOIN conversations c ON c.id=r.conversation_id
                WHERE r.id=%s AND c.user_id=%s""", (run_id, user_id),
            ).fetchone()
            if not owner:
                raise not_found()
            instance = self._instance(connection, user_id, owner["instance_id"], lock=True)
            self._conversation(connection, user_id, owner["id"], lock=True)
            row = connection.execute(
                "SELECT " + RUN_FIELDS + ",r.input,r.retry_key FROM runs r WHERE r.id=%s FOR UPDATE", (run_id,),
            ).fetchone()
            if not row or row["withdrawn_at"]:
                raise not_found()
            self.require_latest_turn(connection, owner["id"], run_id)
            previous_key, payload = row.pop("retry_key"), row.pop("input")
            if previous_key == key and row["attempt"] == expected_attempt + 1:
                return row
            if row["attempt"] != expected_attempt:
                raise ServiceError(409, "turn_changed", "这轮已更新，请重新读取消息。")
            if row["withdrawal_started_at"] or row["status"] in ACTIVE_RUNS:
                raise ServiceError(409, "turn_busy", "请等这轮处理结束后再重来。")
            if connection.execute(
                "SELECT 1 FROM runs WHERE conversation_id=%s AND status IN ('queued','running')", (owner["id"],),
            ).fetchone():
                raise ServiceError(409, "run_in_progress", "会话中还有一轮正在处理。")
            self._require_turn_capacity(connection, user_id, instance)
            original = connection.execute(
                "SELECT content FROM messages WHERE run_id=%s AND role='user' ORDER BY id LIMIT 1", (run_id,),
            ).fetchone()
            if not original or not original["content"].strip() or len(original["content"]) > 12000:
                raise ServiceError(409, "turn_input_unavailable", "这轮缺少可重新提交的问题。")
            payload = {"query": payload.get("query", original["content"]), "targets": payload.get("targets", [])}
            for table in ("run_steps", "run_artifacts"):
                connection.execute(f"DELETE FROM {table} WHERE run_id=%s", (run_id,))
            updated = connection.execute(
                """UPDATE runs AS r SET attempt=attempt+1,retry_key=%s,input=%s,initiator_session_id=%s,
                status='queued',started_at=NULL,finished_at=NULL,worker_id=NULL,lease_expires_at=NULL,
                cancel_requested_at=NULL,error_message=NULL WHERE r.id=%s RETURNING """ + RUN_FIELDS,
                (key, Jsonb(payload), session_id, run_id),
            ).fetchone()
            connection.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (owner["id"],))
            audit(connection, user_id, "run.retry", "queued")
            return updated

        return await self.db.run(retry)

    async def run(self, user_id, run_id):
        def query(connection):
            row = connection.execute(
                "SELECT " + RUN_FIELDS + ",r.lease_expires_at>now() AS lease_active "
                "FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE r.id=%s AND c.user_id=%s FOR SHARE OF r",
                (run_id, user_id),
            ).fetchone()
            if not row:
                raise not_found()
            lease_active = row.pop("lease_active")
            row["steps"], row["messages"], row["active_agents"] = [], [], []
            if row["status"] == "running" and lease_active and row["cancel_requested_at"] is None:
                row["active_agents"] = [item["agent_id"] for item in connection.execute(
                    "SELECT DISTINCT agent_id FROM run_steps WHERE run_id=%s AND status='running' ORDER BY agent_id",
                    (run_id,),
                )]
            elif row["withdrawn_at"] is None and row["status"] not in ACTIVE_RUNS:
                row["steps"] = connection.execute(
                    "SELECT step_id,agent_id,status,result FROM run_steps WHERE run_id=%s ORDER BY step_id", (run_id,),
                ).fetchall()
                row["messages"] = connection.execute(
                    "SELECT " + MESSAGE_FIELDS + """ FROM messages m JOIN runs r ON r.id=m.run_id
                    WHERE m.run_id=%s AND m.role IN ('user','assistant')
                    ORDER BY m.id""", (run_id,),
                ).fetchall()
            return row

        return await self.db.run(query)

    async def cancel(self, user_id, run_id):
        def cancel(connection):
            row = connection.execute(
                "SELECT r.status FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.id=%s AND c.user_id=%s FOR UPDATE OF r",
                (run_id, user_id),
            ).fetchone()
            if not row:
                raise not_found()
            if row["status"] in {"queued", "running"}:
                connection.execute(
                    """UPDATE runs SET cancel_requested_at=now(),
                    finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
                    status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE id=%s""", (run_id,),
                )
                audit(connection, user_id, "run.cancel", "requested")

        await self.db.run(cancel)
        return await self.run(user_id, run_id)

    async def claim(self):
        worker_id = uuid4()  # New fencing identity per execution, even on the same process.

        def claim(connection):
            connection.execute(
                """WITH expired AS (
                    UPDATE runs SET status=CASE WHEN cancel_requested_at IS NULL THEN 'failed' ELSE 'cancelled' END,
                    result_status='uncertain',error_message='execution_lease_expired',finished_at=now(),lease_expires_at=NULL
                    WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<now()) RETURNING id
                ) DELETE FROM run_steps USING expired WHERE run_id=expired.id AND status='running'"""
            )
            row = connection.execute(
                """SELECT r.id FROM runs r JOIN conversations c ON c.id=r.conversation_id
                JOIN agent_instances i ON i.id=c.instance_id JOIN users u ON u.id=c.user_id
                WHERE r.status='queued' AND i.status='active' AND u.is_active
                ORDER BY r.created_at,r.id FOR UPDATE OF r SKIP LOCKED FOR SHARE OF i SKIP LOCKED LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            return connection.execute(
                """UPDATE runs SET status='running',started_at=now(),worker_id=%s,
                lease_expires_at=now()+%s*interval '1 second' WHERE id=%s RETURNING *""",
                (worker_id, self.settings.lease_seconds, row["id"]),
            ).fetchone()

        return await self.db.run(claim)

    async def execution_context(self, run):
        def query(connection):
            context = connection.execute(
                """SELECT r.id AS run_id,r.input,r.attempt,r.result_status,r.created_at,
                c.id AS conversation_id,c.user_id,c.instance_id,
                (SELECT min(m.id) FROM messages m WHERE m.run_id=r.id AND m.role='user') AS turn_start,
                (SELECT m.content FROM messages m WHERE m.run_id=r.id AND m.role='assistant' AND r.attempt>1
                 ORDER BY m.id DESC LIMIT 1) AS previous_answer,
                r.attempt>1 AND EXISTS (SELECT 1 FROM chat_turn_effects e WHERE e.run_id=r.id) AS previous_campus_effect
                FROM runs r JOIN conversations c ON c.id=r.conversation_id
                WHERE r.id=%s AND r.worker_id=%s AND r.status='running'
                AND r.lease_expires_at>now() AND r.cancel_requested_at IS NULL""",
                (run["id"], run["worker_id"]),
            ).fetchone()
            if not context:
                raise not_found()
            history = connection.execute(
                """SELECT m.role,m.content,
                CASE WHEN m.role='assistant' AND r.status='succeeded' AND r.finished_at<=%s
                    THEN r.result_status END AS reply_status,
                CASE WHEN m.role='assistant' AND r.status='succeeded' AND r.finished_at<=%s THEN (
                    SELECT jsonb_agg(a.payload->'rows' ORDER BY a.id) FROM run_artifacts a
                    WHERE a.run_id=r.id AND a.kind='timetable' AND a.payload->>'service'='gym'
                ) END AS gym_timetables,
                s.step_id IS NOT NULL OR (
                    m.role='user' AND r.finished_at<=%s
                    AND EXISTS (SELECT 1 FROM chat_turn_effects e WHERE e.run_id=r.id)
                ) AS campus_action_turn,
                COALESCE(s.status='needs_input' AND r.status='succeeded' AND (
                    s.result->>'classroom_details_pending'='true'
                    OR starts_with(s.result->>'content','校园服务支持预约教室。请在办理指令里写明')
                    OR starts_with(s.result->>'content','预约教室服务暂未就绪。请在办理指令里写明')
                ),FALSE) AS classroom_pending,
                s.result->'classroom_source' AS classroom_source,
                s.result->>'classroom_confirmation'='true' AS classroom_confirmation,
                s.result->>'classroom_target' AS classroom_target,
                COALESCE(s.result->>'compound_reply',s.result->>'content') AS campus_action_reply,
                CASE WHEN s.status='needs_input' AND r.status='succeeded'
                    THEN s.result->'campus_action_draft' END AS campus_action_draft,
                CASE WHEN s.status='needs_input' AND r.status='succeeded'
                    THEN s.result->'pending_campus_action' END AS pending_campus_action
                FROM messages m LEFT JOIN runs r ON r.id=m.run_id
                LEFT JOIN run_steps s ON m.role='user' AND s.run_id=r.id AND s.step_id='minta-action'
                    AND r.status IN ('succeeded','failed') AND r.finished_at<=%s
                WHERE m.conversation_id=%s AND m.role IN ('user','assistant') AND r.withdrawn_at IS NULL
                AND m.run_id IS DISTINCT FROM %s AND (%s::bigint IS NULL OR m.id<%s)
                ORDER BY m.id DESC LIMIT 40""",
                (context["created_at"], context["created_at"], context["created_at"], context["created_at"],
                 context["conversation_id"], context["run_id"],
                 context["turn_start"], context["turn_start"]),
            ).fetchall()
            bounded, characters = [], 0
            for message in history:
                characters += len(message["content"])
                if characters > 60000:
                    break
                bounded.append(message)
            return {"user_id": context["user_id"], "instance_id": context["instance_id"],
                    "conversation_id": context["conversation_id"], "run_id": context["run_id"],
                    "query": context["input"]["query"], "targets": tuple(context["input"]["targets"]),
                    "history": tuple(reversed(bounded)), "worker_id": run["worker_id"], "attempt": context["attempt"],
                    "previous_answer": context["previous_answer"], "previous_result": context["result_status"],
                    "previous_campus_effect": context["previous_campus_effect"]}

        return await self.db.run(query)

    async def heartbeat(self, run):
        def beat(connection):
            row = connection.execute(
                """UPDATE runs SET lease_expires_at=now()+%s*interval '1 second'
                WHERE id=%s AND worker_id=%s AND status='running' AND lease_expires_at>now()
                RETURNING cancel_requested_at""", (self.settings.lease_seconds, run["id"], run["worker_id"]),
            ).fetchone()
            return "lost" if not row else "cancel" if row["cancel_requested_at"] else "ok"

        return await self.db.run(beat)

    async def record_step_activity(self, run, step_id, agent_id, active):
        def update(connection):
            owned = connection.execute(
                """SELECT 1 FROM runs WHERE id=%s AND worker_id=%s AND status='running'
                AND lease_expires_at>now() AND cancel_requested_at IS NULL FOR SHARE""",
                (run["id"], run["worker_id"]),
            ).fetchone()
            if not owned:
                return
            if active:
                connection.execute(
                    """INSERT INTO run_steps(run_id,step_id,agent_id,status,result)
                    VALUES (%s,%s,%s,'running','{}'::jsonb) ON CONFLICT DO NOTHING""",
                    (run["id"], step_id, agent_id),
                )
            else:
                connection.execute(
                    "DELETE FROM run_steps WHERE run_id=%s AND step_id=%s AND status='running'",
                    (run["id"], step_id),
                )

        await self.db.run(update)

    async def finish(self, run, output=None, error=None, artifacts=()):
        def finish(connection):
            error_code = error
            # Lock in the same conversation -> run order as begin_turn.
            conversation = connection.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (run["conversation_id"],)).fetchone()
            if not conversation:
                return False
            current = connection.execute(
                """SELECT *,lease_expires_at>now() AS owned FROM runs
                WHERE id=%s AND conversation_id=%s AND worker_id=%s AND status='running' FOR UPDATE""",
                (run["id"], run["conversation_id"], run["worker_id"]),
            ).fetchone()
            if not current or not current["owned"]:
                return False
            cancelled = current["cancel_requested_at"] is not None or error_code == "execution_cancelled"
            status = "cancelled" if cancelled else "failed" if error_code or output is None else "succeeded"
            outcome = "uncertain" if cancelled else output.result_status if output else ("uncertain" if error_code else None)
            if output and not cancelled and output.result_status == "failed":
                status, error_code = "failed", "agent_failed"
            connection.execute(
                "UPDATE runs SET status=%s,result_status=%s,error_message=%s,finished_at=now(),lease_expires_at=NULL WHERE id=%s",
                (status, outcome, error_code, run["id"]),
            )
            connection.execute("DELETE FROM run_steps WHERE run_id=%s AND status='running'", (run["id"],))
            if output and not cancelled:
                for step in output.steps:
                    connection.execute(
                        "INSERT INTO run_steps(run_id,step_id,agent_id,status,result) VALUES (%s,%s,%s,%s,%s)",
                        (run["id"], step["step_id"], step["agent_id"], step["status"], Jsonb(step["result"])),
                    )
                if output.content:
                    self._save_answer(connection, current, output.content, keep_existing=status == "failed")
                artifacts_to_save = (*artifacts, *output.artifacts)
            else:
                artifacts_to_save = artifacts
            for artifact in artifacts_to_save:
                connection.execute("INSERT INTO run_artifacts(run_id,kind,payload) VALUES (%s,%s,%s)",
                                   (run["id"], artifact["kind"], Jsonb(artifact["payload"])))
            connection.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (run["conversation_id"],))
            return True

        return await self.db.run(finish)

    @staticmethod
    def _save_answer(connection, run, content, *, keep_existing):
        if run["attempt"] > 1:
            existing = connection.execute(
                "SELECT id FROM messages WHERE run_id=%s AND role='assistant' ORDER BY id DESC LIMIT 1", (run["id"],),
            ).fetchone()
            if existing:
                if not keep_existing:
                    connection.execute("UPDATE messages SET content=%s,created_at=now() WHERE id=%s", (content, existing["id"]))
                return
        connection.execute(
            "INSERT INTO messages(conversation_id,run_id,role,content) VALUES (%s,%s,'assistant',%s)",
            (run["conversation_id"], run["id"], content),
        )
