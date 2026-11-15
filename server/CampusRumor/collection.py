"""Run the delivered campus wall collector inside the managed service lifecycle."""

import asyncio
import logging
import os
import re
import sys
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path


LOG = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
DELIVERED_ENTRY = (
    "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.argv=sys.argv[2:]; "
    "runpy.run_path(sys.argv[0],run_name='__main__')"
)


def delivered_command(script, *options):
    # The embedded Windows Python ignores the script directory when resolving imports.
    return [sys.executable, "-B", "-X", "utf8", "-c", DELIVERED_ENTRY, str(script.parent), str(script), *options]


def set_source_status(state, status, *, count=None):
    state["status"] = status
    now = datetime.now(BEIJING).isoformat(timespec="seconds")
    state["checked_at"] = now
    if count is not None:
        state.update(last_success_at=now, last_count=count)


def collector_command(directory, raw, *, pages, environ):
    script = directory / "fetch_history.py"
    if not script.is_file() or raw == directory or not raw.is_relative_to(directory):
        return None
    command = delivered_command(script, "--output", str(raw), "--pages", str(pages))
    if configured := environ.get("YSXY_COLLECTOR_CONFIG", "").strip():
        path = (directory / Path(configured)).resolve()
        return [*command, "--collector-config", str(path)] if path.is_file() else None
    if environ.get("YSXY_HISTORY_URL", "").strip():
        return command
    return command if (directory / "config.json").is_file() else None


async def collect_once(command, directory, *, capture=False):
    process = await asyncio.create_subprocess_exec(*command, cwd=directory,
                                                    stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await process.communicate()
        return process.returncode, output
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            await process.wait()


def _wall_result(raw):
    from app.core.history_status import read_history_status

    record = read_history_status(raw)
    error = record.get("error", "")
    needs_login = isinstance(error, str) and re.search(r"HTTP (401|403)\b|登录|授权", error)
    check_login = isinstance(error, str) and "未返回约定的成功响应" in error
    status = "needs_login" if needs_login else "check_login" if check_login else "unavailable"
    return status, record.get("records", 0)


async def collect_loop(directory, raw, jobs, state, *, pages, interval):
    # Import anything handed over by a local collector, even while the remote source is being configured.
    await asyncio.to_thread(jobs.start, "incremental", "scheduler")
    while True:
        command = collector_command(directory, raw, pages=pages, environ=os.environ)
        if command is None:
            if state.get("status") != "needs_login":
                LOG.warning("校园墙在线采集未就绪。请检查授权采集来源和交付版数据目录；本地增量导入已安排。")
            set_source_status(state, "needs_login")
        else:
            await _collect_wall(command, directory, raw, jobs, state, pages=pages)
        if interval is None:
            return
        await asyncio.sleep(interval)


async def _collect_wall(command, directory, raw, jobs, state, *, pages):
    set_source_status(state, "collecting")
    LOG.info("开始校园墙采集")
    try:
        async with asyncio.timeout(60 + 30 * pages):
            code, _ = await collect_once(command, directory)
    except (OSError, NotImplementedError, TimeoutError) as exc:
        set_source_status(state, "unavailable")
        LOG.warning("无法启动校园墙采集（%s）", type(exc).__name__)
        return
    status, count = _wall_result(raw)
    if code == 0:
        set_source_status(state, "ready", count=count if type(count) is int else 0)
        LOG.info("校园墙本轮采集完成，正在安排增量入库")
        for attempt in range(15):
            _, started = await asyncio.to_thread(jobs.start, "incremental", "scheduler")
            if started:
                break
            if attempt < 14:
                await asyncio.sleep(1)
    else:
        set_source_status(state, "needs_login" if code == 2 else status)
        LOG.warning("校园墙采集退出（%s），请检查采集状态和授权配置", code)


def start_collection_with_app(app, directory, raw, *, pages, interval, xhs_enabled, xhs_profile, xhs_query):
    from fastapi import HTTPException, Request

    from .xhs_collection import collect_xhs_loop

    delivered_lifespan = app.router.lifespan_context

    async def collection_status(request: Request):
        if not request.state.principal.is_admin:
            raise HTTPException(403, "需要管理员权限")
        return request.app.state.collection_status

    app.add_api_route("/api/collection_status", collection_status, methods=["GET"], include_in_schema=False)

    @asynccontextmanager
    async def lifespan(application):
        async with delivered_lifespan(application) as state:
            application.state.xhs._command = partial(delivered_command, directory / "xhs_client.py")
            statuses = {"campus_wall": {"status": "checking"}, "xiaohongshu": {"status": "checking"}}
            application.state.collection_status = statuses
            tasks = [asyncio.create_task(collect_loop(directory, raw, application.state.jobs, statuses["campus_wall"],
                                                     pages=pages, interval=interval)),
                     asyncio.create_task(collect_xhs_loop(directory, xhs_profile, application.state.xhs,
                                                         statuses["xiaohongshu"], query=xhs_query, enabled=xhs_enabled,
                                                         interval=interval))]
            for task, source in zip(tasks, statuses, strict=True):
                def finished(completed, name=source):
                    if not completed.cancelled() and (error := completed.exception()):
                        set_source_status(statuses[name], "unavailable")
                        LOG.error("谣言采集任务 %s 退出（%s）", name, type(error).__name__)
                task.add_done_callback(finished)
            try:
                yield state
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
