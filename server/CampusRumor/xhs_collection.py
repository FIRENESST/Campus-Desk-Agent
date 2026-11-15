"""Collect new XiaoHongShu radar signals using the delivered QR-authorized profile."""

import asyncio
import importlib.util
import json
import re
import sqlite3
from urllib.parse import urlsplit

from .collection import LOG, collect_once, delivered_command, set_source_status


XHS_ORIGIN = "https://www.xiaohongshu.com"
NOTE = re.compile(r"/(?:explore|search_result)/([a-f0-9]{16,})")


def radar_batch(items):
    notes = {}
    for item in items[:20]:
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            address = urlsplit(url)
        except ValueError:
            continue
        match = NOTE.fullmatch(address.path)
        title = item.get("title")
        if address.scheme != "https" or address.netloc != "www.xiaohongshu.com" or not match or not isinstance(title, str):
            continue
        detail = item.get("text")
        if title.strip():
            note_id = match[1]
            notes[note_id] = {"platform": "小红书", "title": title[:150],
                              "detail": detail[:240] if isinstance(detail, str) else "",
                              "url": f"{XHS_ORIGIN}/explore/{note_id}"}
    batch = "".join(json.dumps(notes[note_id], ensure_ascii=False) + "\n" for note_id in sorted(notes))
    return batch.encode("utf-8"), len(notes)


async def _search(directory, bridge, query, state):
    from app.core.storage import StorageError, store_radar

    with bridge.lock:
        if bridge.process is not None or bridge.searching:
            return False
        bridge.searching = True
    signed_in = None
    set_source_status(state, "collecting")
    LOG.info("开始小红书采集")
    try:
        command = delivered_command(directory / "xhs_client.py", f"--search={query}")
        async with asyncio.timeout(55):
            code, output = await collect_once(command, directory, capture=True)
        if not output or len(output) > 64 * 1024:
            raise ValueError("小红书采集回执不可用")
        payload = json.loads(output.splitlines()[-1])
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("小红书采集回执不可用")
        if payload.get("ok") is False:
            signed_in = False
            set_source_status(state, "needs_login")
            LOG.info("小红书采集等待管理员扫码授权")
        elif code == 0 and payload.get("ok") is True:
            signed_in = True
            data, count = radar_batch(payload["items"])
            if count:
                await asyncio.to_thread(store_radar, data, "scheduler")
            set_source_status(state, "ready", count=count)
            LOG.info("小红书采集完成，返回 %s 条雷达信号", count)
        else:
            raise ValueError("小红书采集回执不可用")
    except (OSError, TypeError, ValueError, NotImplementedError, TimeoutError, StorageError, sqlite3.Error) as exc:
        set_source_status(state, "unavailable")
        LOG.warning("小红书采集未完成（%s）", type(exc).__name__)
    finally:
        with bridge.lock:
            bridge.searching = False
            if signed_in is not None:
                bridge.logged_in = signed_in
    return True


async def collect_xhs_loop(directory, profile, bridge, state, *, query, enabled, interval):
    if not enabled:
        set_source_status(state, "disabled")
        return
    if not (directory / "xhs_client.py").is_file() or importlib.util.find_spec("playwright") is None:
        set_source_status(state, "unavailable")
        LOG.warning("小红书采集缺少交付脚本或浏览器依赖")
        return
    clock = asyncio.get_running_loop().time
    due = 0.0
    while True:
        info = bridge.status()
        if info["login_running"]:
            if state["status"] != "awaiting_scan":
                set_source_status(state, "awaiting_scan")
            due = 0.0
        elif not info["searching"]:
            if info["logged_in"] and state["status"] in {"needs_login", "awaiting_scan"}:
                due = 0.0
            authorized_profile = info["logged_in"] or (profile.is_dir() and state["status"] != "needs_login")
            if authorized_profile and clock() >= due:
                if await _search(directory, bridge, query, state):
                    if interval is None and state["status"] != "needs_login":
                        return
                    due = clock() + (interval or 300)
            elif not authorized_profile and state["status"] != "needs_login":
                set_source_status(state, "needs_login")
                LOG.info("小红书采集等待管理员扫码授权")
        await asyncio.sleep(5)
