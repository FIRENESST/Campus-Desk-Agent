import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from .errors import ServiceError
from .runner import ExecutionContext, RunOutput


class WorkerPool:
    def __init__(self, store, runner, settings):
        self.store, self.runner, self.settings = store, runner, settings
        self.tasks = []

    async def start(self):
        self.tasks = [asyncio.create_task(self._loop(), name=f"cloud-worker-{index}")
                      for index in range(self.settings.workers)]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def _loop(self):
        while True:
            try:
                run = await self.store.claim()
                if run:
                    await self.process(run)
                else:
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).warning("Worker operation failed (%s)", type(exc).__name__)
                await asyncio.sleep(1)

    async def _monitor(self, run):
        while True:
            await asyncio.sleep(min(2, self.settings.lease_seconds / 3))
            state = await self.store.heartbeat(run)
            if state != "ok":
                return state

    async def process(self, run):
        state = await self.store.heartbeat(run)
        if state != "ok":
            await self.store.finish(run, error="execution_cancelled" if state == "cancel" else "execution_lease_lost")
            return

        async def report_activity(step_id, agent_id, active):
            try:
                await self.store.record_step_activity(run, step_id, agent_id.value, active)
            except ServiceError as exc:
                logging.getLogger(__name__).debug("Could not report subagent activity (%s)", exc.code)

        @asynccontextmanager
        async def step_activity(step_id, agent_id):
            try:
                await report_activity(step_id, agent_id, True)
                yield
            finally:
                await report_activity(step_id, agent_id, False)

        async def execute():
            async with asyncio.timeout(self.settings.run_timeout):
                values = await self.store.execution_context(run)
                output = await self.runner.run(ExecutionContext(**values, step_activity=step_activity))
                if not isinstance(output, RunOutput):
                    raise ValueError("Runner 必须返回 RunOutput")
                output.validate()
                return output

        execution = asyncio.create_task(execute())
        monitor = asyncio.create_task(self._monitor(run))
        output, error, artifacts = None, None, ()
        stopping = False
        try:
            done, _ = await asyncio.wait((execution, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                state = await monitor
                execution.cancel()
                error = "execution_cancelled" if state == "cancel" else "execution_lease_lost"
            else:
                output = await execution
        except asyncio.CancelledError:
            stopping, error = True, "worker_stopped"
            execution.cancel()
        except TimeoutError:
            error = "execution_timeout"
        except Exception as exc:
            error = "execution_failed"
            snapshot = getattr(exc, "snapshot", None)
            if snapshot:
                artifacts = ({"kind": "failure_snapshot", "payload": asdict(snapshot)},)
        finally:
            monitor.cancel()
            if not execution.done():
                execution.cancel()
            results = await asyncio.gather(execution, monitor, return_exceptions=True)
            if isinstance(results[0], BaseException) and getattr(results[0], "snapshot", None):
                artifacts = ({"kind": "failure_snapshot", "payload": asdict(results[0].snapshot)},)
        try:
            await self.store.finish(run, output, error, artifacts)
        finally:
            if stopping:
                raise asyncio.CancelledError
