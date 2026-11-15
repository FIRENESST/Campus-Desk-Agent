"""Project live campus service records onto a common Beijing-time timetable."""

import asyncio
import re
from datetime import date, datetime, time, timedelta

from ..campus_cluster.action_plan import CAMPUS_TIME_ZONE
from ..campus_cluster.activity_time import campus_datetime
from ..campus_cluster.library_catalog import matches_search, page_slice, search_terms
from ..campus_cluster.subskills.minta_campus import GymSlotsQuery
from .calendar_sync import course_events, personal_calendar
from .errors import ServiceError
from .minta_operations import GatewayRejected, GatewayUncertain, merge_periods
from .roles import UserRole


PAGE_SIZE = 12
LOAN_SERVICES = frozenset({"library", "my_loans"})
UPSTREAM_ERRORS = (GatewayRejected, GatewayUncertain, ValueError, KeyError)
EVENT_KINDS = {"课程": "course", "授课": "teaching", "校园活动": "activity", "教室预约": "classroom_booking", "自定义事件": "custom"}
TIMETABLE_LABELS = {"classroom": "教室时段", "gym": "场馆时段", "activity": "校园活动", "schedule": "课表",
                    "personal": "个人日程", "library": "馆藏借期", "my_loans": "我的借期"}
ROW_STATUS = {"available": "可预约", "occupied": "已占用", "closed": "暂停预约", "unknown": "等待核对",
              "scheduled": "已排期", "ongoing": "进行中", "ended": "已结束", "elapsed": "已开始",
              "due": "计划到期", "overdue": "已过到期时间"}


def _minutes(clock):
    hours, minutes = map(int, clock.split(":"))
    return hours * 60 + minutes


def _windows(query, today):
    first = date.fromisoformat(query.date) if query.date else today
    last = date.fromisoformat(query.end_date) if query.end_date else first
    midnight = datetime.combine(first, time.min, CAMPUS_TIME_ZONE)
    begins, finishes = _minutes(query.start_time), _minutes(query.end_time)
    return tuple((midnight + timedelta(days=day, minutes=begins), midnight + timedelta(days=day, minutes=finishes))
                 for day in range((last - first).days + 1))


def _row(kind, target, start, end, status, **details):
    return {"kind": kind, "resource_id": target["id"], "name": target["name"],
            "starts_at": start.astimezone(CAMPUS_TIME_ZONE), "ends_at": end.astimezone(CAMPUS_TIME_ZONE),
            "status": status, **details}


def _overlaps(start, end, windows):
    return any(start < right and end > left for left, right in windows)


def _classroom_rows(room, periods, windows, now, minimum):
    # Booking starts must be strictly in the future and the reservation form accepts whole minutes.
    next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for start, end in windows:
        if room.get("booking_blocked") or periods is None:
            status = "closed" if room.get("booking_blocked") else "unknown"
            yield _row("classroom", room, start, end, status, capacity=room["capacity"])
            continue

        occupied = merge_periods((max(left, start), min(right, end)) for left, right in periods
                                 if left < end and right > start)
        cursor = start
        for left, right in occupied:
            available_from = max(cursor, next_minute)
            if left - available_from >= minimum:
                yield _row("classroom", room, available_from, left, "available", capacity=room["capacity"])
            yield _row("classroom", room, left, right, "occupied", capacity=room["capacity"])
            cursor = right
        cursor = max(cursor, next_minute)
        if end - cursor >= minimum:
            yield _row("classroom", room, cursor, end, "available", capacity=room["capacity"])


class CampusTimetable:
    def __init__(self, operations):
        self.ops = operations

    async def read(self, who, query, *, now=None):
        now = (now or datetime.now(CAMPUS_TIME_ZONE)).astimezone(CAMPUS_TIME_ZONE)
        windows = _windows(query, now.date())
        lookup = {"classroom": self.classrooms, "gym": self.gym, "activity": self.activities,
                  "schedule": self.schedule, "personal": self.personal, "library": self.library, "my_loans": self.library}
        try:
            rows, details = await lookup[query.service](who, query, windows, now)
        except UPSTREAM_ERRORS:
            raise ServiceError(502, "campus_timetable_unavailable", "暂时无法读取该服务的时段。") from None
        if query.available_only:
            rows = [row for row in rows if row["status"] == "available"]
        if query.service in LOAN_SERVICES:
            rows.sort(key=lambda row: (row["ends_at"], row["name"], row["resource_id"]))
        else:
            rows.sort(key=lambda row: (row["starts_at"], row["name"], int(row.get("court_id", 0)), row["ends_at"]))
        items, page = page_slice(rows, query.page, PAGE_SIZE)
        all_loans = query.service in LOAN_SERVICES and not query.has_window
        return {
            "source": "minta_simulation", "service": query.service, "time_zone": "Asia/Shanghai",
            "as_of": now.isoformat(timespec="seconds"),
            "date": None if all_loans else windows[0][0].date().isoformat(),
            "end_date": None if all_loans else windows[-1][0].date().isoformat(),
            "start_time": query.start_time, "end_time": query.end_time,
            "rows": [{**row, "starts_at": row["starts_at"].isoformat(), "ends_at": row["ends_at"].isoformat()} for row in items],
            "total": len(rows), "page": page, "has_more": page * PAGE_SIZE < len(rows), **details,
        }

    async def classrooms(self, who, query, windows, now):
        catalog = await self.ops.catalog(who, "classroom")
        terms = search_terms(query.target.replace("教室", ""))
        rooms = [room for room in catalog["items"] if room["capacity"] >= query.min_capacity
                 and matches_search(terms, room["name"], room["id"])]
        periods = await self.ops.classroom_periods(rooms, windows[0][0], windows[-1][1])
        minimum = timedelta(minutes=query.min_minutes)
        rows = [row for room in rooms for row in _classroom_rows(
            room, periods[room["id"]] if periods is not None else None, windows, now, minimum,
        )]
        return rows, {"matched_resources": len(rooms), "verified": periods is not None}

    async def gym(self, who, query, windows, now):
        catalog = await self.ops.catalog(who, "gym")
        terms = search_terms("" if query.target.strip() in {"体育馆", "体育场馆", "场馆"} else query.target)
        venues = [venue for venue in catalog["items"] if matches_search(terms, venue["name"], venue["id"])]
        booking_window = catalog.get("booking_window")
        if booking_window is not None:
            windows = [period for period in windows if booking_window["first_date"] <= period[0].date().isoformat()
                       <= booking_window["last_date"]]
        limit = asyncio.Semaphore(6)
        minimum = timedelta(minutes=query.min_minutes)

        async def venue_rows(venue, window):
            day = window[0].date().isoformat()
            try:
                async with limit:
                    payload = await self.ops.catalog(who, "gym", query=GymSlotsQuery(venue_id=venue["id"], date=day))
            except ServiceError:
                return None
            rows = []
            for slot in payload["items"]:
                start, end = (campus_datetime(slot[key]) for key in ("starts_at", "ends_at"))
                if start < window[1] and end > window[0]:
                    status = "elapsed" if start <= now else "available" if slot["remaining"] else "occupied"
                    if status == "available" and min(end, window[1]) - max(start, window[0]) < minimum:
                        continue
                    rows.append(_row("gym", venue, start, end, status, court_id=slot["court_id"],
                                     location=slot["court_name"], capacity=slot["capacity"], remaining=slot["remaining"]))
            return rows

        results = await asyncio.gather(*(venue_rows(venue, window) for window in windows for venue in venues))
        return [row for group in results if group is not None for row in group], {
            "booking_window": booking_window, "matched_resources": len(venues), "verified": all(group is not None for group in results),
        }

    async def activities(self, who, query, windows, now):
        catalog = await self.ops.catalog(who, "club")
        terms = search_terms(query.target)
        rows = []
        for activity in catalog["items"]:
            start, end = (campus_datetime(activity[key]) for key in ("starts_at", "ends_at"))
            if matches_search(terms, activity["name"], activity["location"], activity["id"]) and _overlaps(start, end, windows):
                status = "ended" if end <= now else "ongoing" if start <= now else "scheduled"
                rows.append(_row("activity", activity, start, end, status, location=activity["location"],
                                 remaining=activity["remaining"], capacity=activity["capacity"]))
        return rows, {"verified": True}

    @staticmethod
    def _event_rows(events, query, windows):
        terms = search_terms(query.target)
        return [_row(EVENT_KINDS[event.category], {"id": event.uid, "name": event.title}, event.starts_at, event.ends_at, "scheduled",
                     location=event.location) for event in events
                if matches_search(terms, event.title, event.location) and _overlaps(event.starts_at, event.ends_at, windows)]

    async def schedule(self, who, query, windows, _now):
        events = await course_events(who, self.ops, self.ops.access)
        return self._event_rows(events, query, windows), {"verified": True, "demo_courses": who.role == UserRole.STUDENT}

    async def personal(self, who, query, windows, _now):
        events, gym = await asyncio.gather(
            personal_calendar(who, self.ops, self.ops.access, window=(windows[0][0], windows[-1][1])),
            self.ops.gym_calendar(who), return_exceptions=True,
        )
        metadata = {"verified": True, "demo_courses": who.role == UserRole.STUDENT}
        if isinstance(events, (ServiceError, *UPSTREAM_ERRORS)):
            metadata.update(verified=False)
            events = []
        elif isinstance(events, BaseException):
            raise events
        rows = self._event_rows(events, query, windows)
        if isinstance(gym, ServiceError) and gym.code == "campus_access_denied":
            return rows, metadata
        if isinstance(gym, (ServiceError, *UPSTREAM_ERRORS)):
            return rows, {**metadata, "verified": False}
        if isinstance(gym, BaseException):
            raise gym
        reservations, gym_verified = gym
        terms = search_terms(query.target)
        for booking in reservations:
            target = {"id": booking["resource_id"], "name": booking["resource_name"] or booking["resource_id"]}
            start, end = (campus_datetime(booking[key]) for key in ("starts_at", "ends_at"))
            if matches_search(terms, target["name"], booking["court_name"]) and _overlaps(start, end, windows):
                rows.append(_row("gym_booking", target, start, end, "scheduled", location=booking["court_name"]))
        return rows, {**metadata, "verified": metadata["verified"] and gym_verified}

    async def library(self, who, query, windows, now):
        terms = search_terms(query.target)
        rows, missing_dates = [], 0
        personal = query.service == "my_loans"
        verified = True
        if personal:
            books, verified = await self.ops.library_calendar(who)
        else:
            books = (await self.ops.catalog(who, "library"))["items"]
        for book in books:
            if personal:
                if book["state"] not in {"active", "closing", "close_uncertain"}:
                    continue
                book_id, title = book["resource_id"], book["resource_name"] or book["resource_id"]
                author, loans = "", [book] if book["borrowed_at"] and book["due_at"] else []
                missing = 1 - len(loans)
            else:
                book_id, title, author = (book[key] for key in ("id", "title", "author"))
                loans = book["loan_progress"]
                missing = book["borrowed_copies"] - len(loans)
            if not matches_search(terms, title, book_id, author):
                continue
            missing_dates += missing
            for loan in loans:
                start, end = (campus_datetime(loan[key]) for key in ("borrowed_at", "due_at"))
                if not query.has_window or any(left <= end < right for left, right in windows):
                    rows.append(_row(query.service, {"id": book_id, "name": title}, start, end,
                                     "overdue" if end < now else "due", duration_days=loan["duration_days"]))
        return rows, {"verified": verified, "unknown_due_count": missing_dates}


def _cell(value):
    return re.sub(r"([\\`*_\[\]|~!<>])", r"\\\1", " ".join(value.split()))


def _display_time(value):
    stamp = datetime.fromisoformat(value)
    return stamp.isoformat(sep=" ", timespec="seconds" if stamp.second else "minutes").removesuffix("+08:00")


def format_timetable(receipt, *, available_only=False):
    period = receipt["date"]
    if period and receipt["end_date"] != period:
        period += " 至 " + receipt["end_date"]
    heading = f"**{TIMETABLE_LABELS[receipt['service']]}**（北京时间）"
    if period:
        heading += f" · {period} · {receipt['start_time']}–{receipt['end_time']}"
    lines = [heading, ""]
    rows = receipt["rows"]
    if rows:
        columns = "借出 | 到期" if receipt["service"] in LOAN_SERVICES else "开始 | 结束"
        lines.extend([f"| 项目 | {columns} | 状态 |", "| --- | --- | --- | --- |"])
        for row in rows:
            name = _cell(row["name"])
            if row.get("location"):
                name += " · " + _cell(row["location"])
            status = ROW_STATUS[row["status"]]
            if row.get("remaining") is not None:
                status += f" · 剩余 {row['remaining']}"
            elif row["kind"] == "classroom":
                status += f" · 可容纳 {row['capacity']} 人"
            lines.append(f"| {name} | {_display_time(row['starts_at'])} | {_display_time(row['ends_at'])} | {status} |")
    elif not receipt["verified"]:
        lines.append("暂时无法核对这段时间的安排。")
    elif receipt.get("matched_resources") == 0:
        lines.append("当前账号可见的资源中没有符合条件的项目。")
    elif available_only:
        lines.append("这段时间没有符合条件的可预约时段。")
    else:
        lines.append("查询范围内没有记录。")
    if (window := receipt.get("booking_window")) and (receipt["date"] < window["first_date"]
                                                       or receipt["end_date"] > window["last_date"]):
        lines.extend(["", f"场馆当前放出的预约日期为 {window['first_date']} 至 {window['last_date']}。"])
    if rows and not receipt["verified"]:
        lines.extend(["", "部分时段暂时无法核对，表中列出已取得的记录。"])
    if receipt.get("unknown_due_count"):
        lines.extend(["", f"另有 {receipt['unknown_due_count']} 笔借阅缺少可核对的到期时间。"])
    if receipt.get("demo_courses") and any(row["kind"] == "course" for row in rows):
        lines.extend(["", "学生课程来自本地配置的模拟教务服务。"])
    if receipt["has_more"] or receipt["page"] > 1:
        lines.extend(["", f"第 {receipt['page']} 页，共 {receipt['total']} 条。"])
    return "\n".join(lines)
