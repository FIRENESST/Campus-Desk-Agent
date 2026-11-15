"""Main-agent scheduling only; routing and peer learning are separate ports."""

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace

from ..reliability.models import RunSnapshot
from .contracts import (
    AgentId, AgentReply, AgentTask, ClusterConfig, ClusterError, ClusterRequest, ClusterResult,
    ClusterTimeoutError, RoutePlan, Status, StepResult, UpstreamOutput, aggregate_status,
)
from .registry import AgentRegistry
from .routing import ExplicitRouter, Router, validate_plan


StepActivity = Callable[[str, AgentId], AbstractAsyncContextManager[None]]


def task_payload(task: AgentTask) -> dict:
    return {
        "conversation_id": task.conversation_id,
        "step_id": task.step_id,
        "agent_id": task.profile.id.value,
        "query": task.query,
        "resolved_query": task.resolved_query,
        "history": [asdict(message) for message in task.history],
        "evidence": [asdict(item) for item in task.evidence],
        "upstream": [asdict(item) for item in task.upstream],
    }


class CampusCluster:
    def __init__(
        self, registry: AgentRegistry, *, router: Router | None = None,
        config: ClusterConfig | None = None,
    ):
        self.registry = registry
        self.router = router if router is not None else ExplicitRouter()
        self.config = config if config is not None else ClusterConfig()

    async def run(
        self, request: ClusterRequest, *, on_dispatch: Callable[[AgentId], None] | None = None,
        step_activity: StepActivity | None = None, prepared_plan: RoutePlan | None = None,
        step_capacity: asyncio.Semaphore | None = None,
    ) -> ClusterResult:
        if not isinstance(request, ClusterRequest):
            raise ValueError("request 必须是 ClusterRequest")
        request = deepcopy(request)
        self._bound({
            "query": request.query,
            "history": [asdict(item) for item in request.history],
            "evidence": [asdict(item) for item in request.evidence],
        })
        results: dict[str, StepResult] = {}
        plan = RoutePlan()
        try:
            async with asyncio.timeout(self.config.total_timeout):
                if prepared_plan is None:
                    prepared_plan = await self.router.route(deepcopy(request), self.registry.profiles)
                plan = deepcopy(prepared_plan)
                validate_plan(plan, max_steps=self.config.max_steps)
                if plan.direct or plan.parts:
                    return ClusterResult(request.conversation_id, Status.COMPLETED, plan)
                if plan.campus_action is not None:
                    return ClusterResult(request.conversation_id, Status.NEEDS_INPUT, plan)
                if plan.timetable_query is not None:
                    if on_dispatch:
                        on_dispatch(AgentId.CAMPUS_SERVICES)
                    return ClusterResult(request.conversation_id, Status.COMPLETED, plan)
                if plan.clarification is not None:
                    return ClusterResult(
                        request.conversation_id, Status.NEEDS_INPUT, plan,
                        clarification=plan.clarification,
                    )
                if plan.service_guide is not None:
                    request = replace(request, evidence=(*request.evidence, plan.service_guide))
                capacity = step_capacity if step_capacity is not None else asyncio.Semaphore(self.config.max_parallel)
                await self._execute(request, plan, results, capacity, on_dispatch, step_activity)
        except TimeoutError as exc:
            raise ClusterTimeoutError(self._ordered(plan, results)) from exc
        except asyncio.CancelledError as exc:
            exc.results = self._ordered(plan, results)
            raise
        ordered = self._ordered(plan, results)
        return ClusterResult(request.conversation_id, aggregate_status({item.reply.status for item in ordered}), plan, ordered)

    async def _execute(self, request, plan, results, semaphore, on_dispatch, step_activity):
        pending = {step.id: step for step in plan.steps}
        while pending:
            ready = [step for step in pending.values() if set(step.depends_on) <= results.keys()]
            if not ready:
                raise ClusterError("路由计划无法继续执行")
            async with asyncio.TaskGroup() as group:
                for step in ready:
                    group.create_task(self._run_step(
                        request, step, results, semaphore, on_dispatch, step_activity, resolved_query=plan.resolved_query,
                    ))
            for step in ready:
                del pending[step.id]

    async def _run_step(self, request, step, results, semaphore, on_dispatch, step_activity, *, resolved_query):
        dependencies = [results[step_id] for step_id in step.depends_on]
        if any(item.reply.status != Status.COMPLETED for item in dependencies):
            results[step.id] = StepResult(
                step.id, step.agent_id,
                AgentReply(Status.BLOCKED, reason="dependency_not_completed"),
            )
            return
        agent = self.registry.get(step.agent_id)
        evidence = tuple(
            replace(item, id=f"input:{index}") for index, item in enumerate(request.evidence, 1)
        )
        for dependency in dependencies:
            evidence += tuple(
                replace(item, id=f"upstream:{dependency.step_id}:{index}")
                for index, item in enumerate(dependency.reply.evidence, 1)
            )
        task = AgentTask(
            request.conversation_id, step.id, agent.profile, request.query,
            deepcopy(request.history), evidence,
            tuple(UpstreamOutput(item.step_id, item.agent_id, item.reply.content) for item in dependencies),
            resolved_query=resolved_query,
        )
        async with semaphore:
            try:
                self._bound(task_payload(task))
                activity = step_activity(step.id, step.agent_id) if step_activity else nullcontext()
                async with activity, asyncio.timeout(self.config.step_timeout):
                    if on_dispatch is not None:
                        on_dispatch(step.agent_id)
                    reply = await agent.run(task)
                snapshot = reply.snapshot or (reply.reliability.snapshot if reply.reliability else None)
                result = StepResult(step.id, step.agent_id, reply, snapshot=snapshot)
            except asyncio.CancelledError as exc:
                results[step.id] = self._failure(step, "agent_cancelled", exc)
                raise
            except TimeoutError as exc:
                result = self._failure(step, "agent_timeout", exc)
            except Exception as exc:
                result = self._failure(step, "agent_error", exc)
            results[step.id] = result

    def _bound(self, payload):
        try:
            content = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ClusterError("集群上下文不能序列化为标准 JSON") from exc
        if len(content) > self.config.max_context_chars:
            raise ClusterError("集群上下文超过字符预算")

    @staticmethod
    def _failure(step, reason, error):
        snapshot = getattr(error, "snapshot", None)
        if snapshot is None and isinstance(error, TimeoutError) and isinstance(error.__cause__, asyncio.CancelledError):
            snapshot = getattr(error.__cause__, "snapshot", None)
        return StepResult(
            step.id, step.agent_id, AgentReply(Status.FAILED, reason=reason),
            type(error).__name__, snapshot if isinstance(snapshot, RunSnapshot) else None,
        )

    @staticmethod
    def _ordered(plan, results):
        return tuple(results[step.id] for step in plan.steps if step.id in results)
