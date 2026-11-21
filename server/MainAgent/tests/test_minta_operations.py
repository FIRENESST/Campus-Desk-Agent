import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import test_cloud


def classroom_times(hour=14):
    local = datetime.now(UTC) + timedelta(hours=8, days=2)
    day = local.date()
    return {"starts_at": f"{day}T{hour:02d}:00", "ends_at": f"{day}T{hour + 1:02d}:00"}


if test_cloud.ENABLED:
    import httpx2

    from main_agent.campus_cluster.agents.campus_services import CampusServicesAgent
    from main_agent.campus_cluster.contracts import AgentTask
    from main_agent.cloud.api import create_app as create_cloud_app
    from main_agent.cloud.auth import set_user_role
    from main_agent.cloud.minta_chat import CampusChat
    from main_agent.cloud.minta_operations import GatewayRejected, GatewayUncertain, OPERATIONS
    from main_agent.cloud.runner import CampusRunner
    from main_agent.cloud.security import digest
    from main_agent.cloud.worker import WorkerPool
    from main_agent.core import Message
    from main_agent.teacher_credentials import TeacherCredentialClient, create_app, credential_digest, read_key


class CampusFixture:
    def __init__(self):
        self.records = {service: {} for service in OPERATIONS}
        self.books = [{"id": "book-one", "title": "馆藏", "author": "作者", "total_copies": 2,
                       "category": "文学与小说", "category_id": "literature"}]
        self.lose_reply = False
        self.writes = []
        self.tool_names = {service: {spec.create, spec.close} for service, spec in OPERATIONS.items()}
        self.tool_list_reads = []

    async def request(self, method, path):
        url = urlsplit(path)
        params = parse_qs(url.query)
        if url.path == "/gym/api/slots":
            return {"slots": [self.slot(params["date"][0], "1", "0900"), self.slot(params["date"][0], "2", "1000")]}
        if path == "/gym/api/venues":
            return {"venues": [{"id": "strength", "name": "力量训练区", "capacity": 2, "remaining": 2, "court_count": 2}],
                    "booking_window": {"first_date": "2026-11-13", "last_date": "2026-11-19"}}
        service, route = path[1:].split("/", 1)
        if route == "agent/tools":
            self.tool_list_reads.append(service)
            options = {"gym": {"slot_id": {}}, "classroom": {"starts_at": {}, "ends_at": {}}}
            return {"tools": [{"name": name, "parameters": {
                "properties": options.get(service, {}) if name == OPERATIONS[service].create else {},
            }} for name in self.tool_names[service]]}
        if route == "managed/capabilities":
            return {"actions": ["register"]}
        if route == "api/availability":
            occupied = {record["roomId"] for record in self.records["classroom"].values()}
            return {"rooms": [{"roomId": f"room-a{number}", "name": f"明理楼 A{number}", "capacity": 36,
                               "reserved": f"room-a{number}" in occupied} for number in (101, 102, 103)]}
        if route == "api/books":
            loans = list(self.records["library"].values())
            books = []
            for book in self.books:
                active = [loan for loan in loans if loan["book_id"] == book["id"]]
                books.append({**book, "available_copies": book["total_copies"] - len(active),
                              "borrower_name": "private", "loan_progress": active})
            return {"books": books}
        spec = OPERATIONS[service]
        assert "/" + url.path.split("/", 2)[-1] == spec.records
        return {spec.records_key: list(self.records[service].values())}

    @staticmethod
    def slot(day, court, clock):
        return {"slot_id": f"{day}_{court}_{clock}", "court_id": court, "court_name": f"{court} 号训练区",
                "starts_at": f"{day}T{clock[:2]}:00:00+08:00", "ends_at": f"{day}T{int(clock[:2])+1:02d}:00:00+08:00",
                "capacity": 1, "remaining": 1}

    async def tool(self, service, name, args):
        self.writes.append((service, name, args))
        spec, records = OPERATIONS[service], self.records[service]
        if name == spec.create:
            receipt = {spec.id_field: str(uuid4()), spec.resource_field: args[spec.resource_arg],
                       spec.actor_field: args[spec.actor_arg]}
            if service == "library":
                started = datetime.now(UTC)
                days = args["duration_days"]
                title = next((book["title"] for book in self.books if book["id"] == args["book_id"]), None)
                receipt.update(duration_days=days, borrowed_at=started.isoformat(),
                               due_at=(started + timedelta(days=days)).isoformat(), book_title=title)
            elif service == "gym":
                receipt.update(self.slot(*args["slot_id"].split("_")), venue_name="力量训练区")
            elif service == "classroom":
                receipt.update(startsAt=args["starts_at"], endsAt=args["ends_at"], roomName="明理楼 " + args["room_id"][5:].upper())
            records[receipt[spec.id_field]] = receipt
            wrapped = spec.receipt
        else:
            assert name == spec.close
            receipt = records.pop(args[spec.close_arg])
            wrapped = spec.receipt or "reservation"
        if self.lose_reply:
            self.lose_reply = False
            raise GatewayUncertain
        return {wrapped: receipt} if wrapped else receipt


def action_review(status, rule_id="campus.action_plan"):
    return Message("assistant", json.dumps({
        "rule_checks": [{"rule_id": rule_id, "status": status, "reason": "已核对本轮委托"}],
        "claims": [], "issues": [],
    }))


class CampusOperationsTests(test_cloud.CloudPostgresTestCase):
    async def asyncSetUp(self):
        import os
        self.enterContext(patch.dict(os.environ, {"CLOUD_TERM_ID": "fixture-term",
                          "CLOUD_TERM_NAME": "测试学期", "CLOUD_TERM_START": "2026-09-07"}))
        await super().asyncSetUp()

        async def number(who):
            await self.app.state.campus_access.check(who, "schedule")
            return "student-test-a" if who.role.value == "student" else None

        self.enterContext(patch.object(self.app.state.campus_access, "schedule_number", number))

    def extra_app_arguments(self):
        self.gateway = CampusFixture()
        return {"minta_gateway": self.gateway}

    async def test_classroom_timeline_clips_the_day_and_shares_only_busy_minutes(self):
        path = "/v1/minta/catalog/classroom"
        day = "2026-11-15"
        self.assertEqual((await self.client.get(path, params={"date": day})).status_code, 401)
        headers, _ = await self.account()
        calls = []
        original = self.gateway.request

        async def track_window(method, target):
            if target.startswith("/classroom/api/reservations"):
                calls.append(parse_qs(urlsplit(target).query))
            return await original(method, target)

        for room_id, reservation_id, start, end in (
            ("room-a101", "private-a", "2026-11-14T23:30:00+08:00", f"{day}T00:30:00+08:00"),
            ("room-a101", "private-b", f"{day}T00:30:00+08:00", f"{day}T01:00:00+08:00"),
            ("room-a101", "private-c", f"{day}T23:30:00+08:00", "2026-11-16T01:00:00+08:00"),
            ("room-a102", "private-d", None, None),
            ("room-a103", "private-e", "2026-11-16T00:00:00+08:00", "2026-11-16T01:00:00+08:00"),
        ):
            self.gateway.records["classroom"][reservation_id] = {
                "roomId": room_id, "reservationId": reservation_id, "startsAt": start,
                "endsAt": end, "userName": "仅后端可见的登记人",
            }

        with patch.object(self.gateway, "request", track_window):
            response = await self.client.get(path, params={"date": day}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [{"ends_after": [f"{day}T00:00:00+08:00"],
                                  "starts_before": ["2026-11-16T00:00:00+08:00"]}])
        self.assertEqual(response.json()["date"], day)
        self.assertEqual({item["id"]: item["busy_slots"] for item in response.json()["items"]}, {
            "room-a101": [{"start_minute": 0, "end_minute": 60}, {"start_minute": 1410, "end_minute": 1440}],
            "room-a102": [{"start_minute": 0, "end_minute": 1440}],
            "room-a103": [],
        })
        self.assertNotIn("private-", response.text)
        self.assertNotIn("登记人", response.text)
        self.assertEqual((await self.client.get(path + "?date=2026-02-30", headers=headers)).status_code, 422)
        self.assertEqual((await self.client.get(path + f"?date={day}&date={day}", headers=headers)).status_code, 422)
        old_catalog = (await self.client.get(path, headers=headers)).json()
        self.assertNotIn("busy_slots", old_catalog["items"][0])

        self.gateway.records["classroom"].pop("private-c")
        refreshed = await self.client.get(path, params={"date": day}, headers=headers)
        self.assertEqual(refreshed.json()["items"][0]["busy_slots"], [{"start_minute": 0, "end_minute": 60}])

        async def unavailable(method, target):
            if target.startswith("/classroom/api/reservations"):
                raise GatewayUncertain
            return await original(method, target)

        with patch.object(self.gateway, "request", unavailable):
            unknown = await self.client.get(path, params={"date": day}, headers=headers)
        self.assertEqual(unknown.status_code, 200, unknown.text)
        self.assertIsNone(unknown.json()["items"][0]["busy_slots"])

    async def test_campus_timetable_uses_the_same_live_occupancy_in_http_and_chat(self):
        path = "/v1/minta/timetable"
        day = classroom_times()["starts_at"][:10]
        query = {"service": "classroom", "date": day, "target": "A101", "start_time": "13:00",
                 "end_time": "17:00", "min_minutes": 60, "available_only": True}
        self.gateway.records["classroom"]["anonymous-room-booking"] = {
            "roomId": "room-a101", "reservationId": "anonymous-room-booking", "userName": "需要保密的预约人",
            "startsAt": f"{day}T14:00:00+08:00", "endsAt": f"{day}T15:30:00+08:00",
        }
        self.assertEqual((await self.client.get(path, params=query)).status_code, 401)
        headers, _ = await self.account()
        response = await self.client.get(path, params=query, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("需要保密的预约人", response.text)
        self.assertNotIn("anonymous-room-booking", response.text)
        self.assertEqual([(row["starts_at"], row["ends_at"], row["status"]) for row in response.json()["rows"]], [
            (f"{day}T13:00:00+08:00", f"{day}T14:00:00+08:00", "available"),
            (f"{day}T15:30:00+08:00", f"{day}T17:00:00+08:00", "available"),
        ])
        self.assertEqual((await self.client.get(path, params={**query, "date": "2026-02-30"}, headers=headers)).status_code, 422)

        async def complete(messages, tools):
            self.assertFalse(tools)
            system = messages[0].content
            if system.startswith("你是 MainAgent。"):
                return Message("assistant", '{"decision":"other"}')
            if "范围判断" in system:
                return Message("assistant", '{"campus":true}')
            self.assertIn("campus_timetable", system)
            return Message("assistant", json.dumps({"timetable_query": query}))

        async def local_capabilities(registrations):
            return registrations

        _, conversation = await self.conversation(headers)
        runner = CampusRunner.with_model(SimpleNamespace(complete=complete),
                                         campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth))
        worker = WorkerPool(self.store, runner, self.config)
        sent = await self.submit(headers, conversation, f"{day}下午一点到五点 A101 有哪些至少一小时的空闲时段？")
        self.assertEqual(sent.status_code, 202, sent.text)
        with patch("main_agent.cloud.runner.load_subagent_capabilities", local_capabilities):
            await worker.process(await self.store.claim())
        lookup = await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=headers)
        self.assertEqual(lookup.json()["result_status"], "completed", lookup.text)
        self.assertIn("明理楼 A101", lookup.json()["messages"][-1]["content"])
        self.assertIn("15:30", lookup.json()["messages"][-1]["content"])
        self.assertNotIn("需要保密的预约人", lookup.text)
        self.assertFalse(self.gateway.writes)

        original = self.gateway.request

        async def unavailable(method, target):
            if target.startswith("/classroom/api/reservations"):
                raise GatewayUncertain
            return await original(method, target)

        with patch.object(self.gateway, "request", unavailable):
            unknown = await self.client.get(path, params=query, headers=headers)
        self.assertFalse(unknown.json()["verified"])
        self.assertFalse(unknown.json()["rows"])

    async def test_startup_reads_current_action_registrations(self):
        @asynccontextmanager
        async def stub_model():
            yield self.runner

        spec = OPERATIONS["classroom"]
        with (
            patch("main_agent.cloud.api.connect_llm_server", stub_model),
            patch("main_agent.cloud.api.publish_default_capabilities") as publish,
            patch("main_agent.cloud.api.CampusRunner.with_model", wraps=CampusRunner.with_model) as build,
        ):
            for present in (False, True):
                self.gateway.tool_names["classroom"] = {spec.create} if present else set()
                reads = len(self.gateway.tool_list_reads)
                app = create_cloud_app(self.config, mailer=self.mailer, minta_gateway=self.gateway)
                async with app.router.lifespan_context(app):
                    self.assertCountEqual(self.gateway.tool_list_reads[reads:], OPERATIONS)
                    campus = build.call_args.kwargs["campus_chat"]
                    self.assertEqual("classroom.start" in await campus.registered_actions(), present)
            self.assertEqual(publish.call_count, 2)

    async def test_discovery_uses_live_registrations(self):
        campus = CampusChat(self.app.state.campus_ops, self.app.state.auth)
        self.gateway.tool_names["classroom"].remove(OPERATIONS["classroom"].create)
        with patch("main_agent.cloud.minta_chat.monotonic", return_value=100.0) as now:
            actions = await campus.registered_actions()
            self.assertNotIn("classroom.start", actions)
            self.assertIn("classroom.close", actions)
            self.assertIn("clubs.register", actions)
            self.gateway.tool_names["classroom"].add(OPERATIONS["classroom"].create)
            self.assertNotIn("classroom.start", await campus.registered_actions())
            now.return_value += 6
            self.assertIn("classroom.start", await campus.registered_actions())

    async def start(self, headers, service, resource_id, *, key=None, teacher=None, duration_days=None,
                    slot_id=None, identity_confirmed=True, times=None):
        data = {"service": service, "resource_id": resource_id}
        if teacher is not None:
            data["teacher"] = teacher
        if duration_days is not None:
            data["duration_days"] = duration_days
        if service == "gym":
            data.update(slot_id=slot_id or "2026-11-14_1_0900", identity_confirmed=identity_confirmed)
        if service == "classroom":
            data.update(classroom_times() if times is None else times)
        return await self.client.post("/v1/minta/operations", json=data,
                                      headers={**headers, "Idempotency-Key": key or str(uuid4())})

    async def test_semester_report_is_private_and_updates_from_confirmed_registrations(self):
        path = "/v1/minta/semester-report"
        alice, _ = await self.account("alice")
        bob, _ = await self.account("bob")
        course = {"course_id": "C101", "course_name": "传播学", "points": 3, "student_id": "private"}
        catalog = [
            {"id": "this-term", "name": "校园外拍", "starts_at": "2026-09-06T16:00:00Z", "points": 4},
            {"id": "next-term", "name": "下学期活动", "starts_at": "2027-01-24T16:00:00Z", "points": 2},
        ]
        by_id = {event["id"]: event for event in catalog}
        original_tool, original_request = self.gateway.tool, self.gateway.request
        checked_participants = []

        async def tool(service, name, arguments):
            if service == "schedule":
                self.assertEqual((name, arguments), ("schedule_query", {"student_id": "student-test-a"}))
                return {"courses": [course, {**course, "classroom": "第二场"}, {
                    **course, "course_id": "C102", "course_name": "大学英语", "points": 1,
                }]}
            if name == "club_list_activities":
                return {"activities": [{**item, "description": "校园活动", "location": "学校",
                    "ends_at": "2027-01-26T13:00+08:00", "capacity": 5, "remaining": 5} for item in catalog]}
            return await original_tool(service, name, arguments)

        async def request(method, target, payload=None):
            if (method, target) == ("POST", "/club/managed/registrations"):
                checked_participants.append(set(payload["participants"]))
                # Deliberately return the full fixture so MainAgent must enforce ownership too.
                return {"registrations": [{
                    **record, "activity_name": by_id[record["activity_id"]]["name"],
                    "starts_at": by_id[record["activity_id"]]["starts_at"], "points": by_id[record["activity_id"]]["points"],
                } for record in self.gateway.records["club"].values()]}
            if (method, urlsplit(target).path) == ("GET", "/club/managed/registrations"):
                return {**await original_request(method, "/club/managed/registrations"), "purged_activity_ids": []}
            if (method, target) == ("GET", "/schedule/api/teaching/t002"):
                return {"courses": [{"course_id": "C205", "course_name": "新闻实务", "points": 2}]}
            return await original_request(method, target)

        async def report(headers):
            response = await self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            return response.json()

        with patch.object(self.gateway, "tool", tool), patch.object(self.gateway, "request", request):
            self.assertEqual((await self.client.get(path)).status_code, 401)
            self.assertEqual((await self.client.get(path + "?user_id=other", headers=alice)).status_code, 422)
            before = await report(alice)
            self.assertEqual(before["term"]["ends_on"], "2027-01-24")
            self.assertEqual((before["courses"]["points"], before["total_points"]), (4, 4))
            self.assertEqual(len(before["courses"]["items"]), 2)
            for item in before["courses"]["items"]:
                self.assertEqual(set(item), {"course_id", "course_name", "points", "grade"})
                self.assertIn(item["grade"], range(60, 101))
            self.assertEqual((await report(alice))["courses"], before["courses"])
            listed = (await self.client.get("/v1/minta/activities", headers=alice)).json()["items"]
            self.assertEqual([item["points"] for item in listed], [4, 2])

            self.assertEqual((await self.start(bob, "club", "this-term")).status_code, 200)
            self.assertEqual((await report(alice))["activities"]["items"], [])
            other = await report(bob)
            self.assertEqual(other["activities"]["points"], 4)
            bob_actors = checked_participants[-1]
            self.assertNotEqual(bob_actors, set())

            self.assertEqual((await self.start(alice, "club", "next-term")).status_code, 200)
            self.assertEqual((await report(alice))["total_points"], 4)
            self.gateway.lose_reply = True
            registration = await self.start(alice, "club", "this-term")
            self.assertEqual(registration.status_code, 202)
            signed_up = await report(alice)
            self.assertTrue(checked_participants[-1].isdisjoint(bob_actors))
            self.assertEqual(signed_up["courses"], before["courses"])
            self.assertEqual(signed_up["activities"]["items"], [{
                "id": "this-term", "name": "校园外拍", "starts_at": "2026-09-07T00:00:00+08:00", "points": 4,
            }])
            self.assertEqual(signed_up["total_points"], 8)

            listing = await self.client.get("/v1/minta/operations?service=club", headers=alice)
            self.assertEqual(listing.status_code, 200, listing.text)
            operation = next(item for item in listing.json()["items"] if item["resource_id"] == "this-term")
            self.assertEqual(operation["state"], "active")
            reply = await self.client.delete(f"/v1/minta/operations/{operation['id']}", headers=alice)
            self.assertEqual(reply.json()["state"], "closed")
            self.assertEqual((await report(alice))["total_points"], 4)
            self.assertEqual(await report(bob), other)

            with patch.object(self.app.state.campus_access, "schedule_number", side_effect=GatewayUncertain):
                partial = await report(bob)
            self.assertEqual(partial["courses"], {"items": [], "points": None, "status": "unavailable"})
            self.assertIsNone(partial["total_points"])
            self.assertEqual(partial["activities"], other["activities"])

            await asyncio.to_thread(set_user_role, self.admin_dsn, "bob@gmail.com", "teacher", "讲师")
            unverified = await report(bob)
            self.assertEqual((unverified["audience"], unverified["courses"]["status"]), ("teacher", "unverified"))
            with patch.object(self.app.state.campus_access, "schedule_number", return_value="t002"):
                teaching = await report(bob)
            self.assertEqual([item["course_id"] for item in teaching["courses"]["items"]], ["C205"])
            self.assertEqual(teaching["total_points"], 6)

    async def test_calendar_events_tracks_timed_classroom_bookings_and_private_edits(self):
        alice, _ = await self.account("alice")
        bob, _ = await self.account("bob")
        period = classroom_times()
        later = classroom_times(15)
        day = datetime.fromisoformat(period["starts_at"]).date()
        path = "/v1/calendar/events"
        week = f"{path}?start={day}&end={day + timedelta(days=1)}"
        event_id = str(uuid4())
        event = {"id": event_id, "title": "个人讨论", "location": "实验室", **period}
        item_path = f"{path}/{event_id}"
        self.assertEqual((await self.client.get(week)).status_code, 401)
        self.assertEqual((await self.client.get(path, headers=alice)).status_code, 422)
        self.assertEqual((await self.client.post(path, headers=alice, json={**event, "ends_at": period["starts_at"]})).status_code, 422)
        created = await self.client.post(path, headers=alice, json=event)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual((await self.client.post(path, headers=alice, json=event)).json(), created.json())
        self.assertEqual((await self.client.get(week, headers=bob)).json()["items"], [])
        self.assertEqual((await self.client.delete(f"{item_path}?revision=1", headers=bob)).status_code, 404)
        changes = {key: value for key, value in event.items() if key != "id"}
        changed = await self.client.put(item_path, headers=alice, json={**changes, **later, "revision": 1})
        self.assertEqual(changed.json()["revision"], 2, changed.text)
        self.assertEqual((await self.client.delete(f"{item_path}?revision=1", headers=alice)).status_code, 409)

        with TemporaryDirectory(prefix="minta-calendar-period-test-") as folder:
            directory = Path(folder)
            read_key(directory, create=True)

            async def verify(_kind, _data):
                return {"id": str(uuid4())}

            self.app.state.campus_ops.credentials = SimpleNamespace(directory=directory, verify=verify)
            contact = {"name": "测试教师", "phone": "13900001234"}
            missing = await self.start(alice, "classroom", "room-a101", teacher=contact, times={})
            self.assertEqual(missing.status_code, 422, missing.text)
            key = str(uuid4())
            booking = await self.start(alice, "classroom", "room-a101", teacher=contact, key=key, times=period)
            self.assertEqual(booking.json()["state"], "active", booking.text)
            self.assertEqual((await self.start(alice, "classroom", "room-a101", teacher=contact, key=key,
                                               times=period)).json(), booking.json())
            self.gateway.lose_reply = True
            adjacent = await self.start(alice, "classroom", "room-a101", teacher=contact, times=later)
            self.assertEqual(adjacent.status_code, 202, adjacent.text)
            items = (await self.client.get(week, headers=alice)).json()["items"]
            self.assertEqual({item["id"] for item in items}, {event_id, booking.json()["id"], adjacent.json()["id"]})
            self.assertEqual((await self.client.get(week, headers=bob)).json()["items"], [])
            self.assertNotIn(contact["phone"], str(items))
            other_week = f"{path}?start={day + timedelta(days=1)}&end={day + timedelta(days=8)}"
            self.assertEqual((await self.client.get(other_week, headers=alice)).json()["items"], [])

            original = self.gateway.request
            original_tool = self.gateway.tool

            async def schedule(service, name, data):
                if service == "schedule":
                    return {"items": [], "courses": [], "periods": []}
                return await original_tool(service, name, data)

            async def offline(method, target):
                if target.startswith("/classroom/api/reservations"):
                    raise GatewayUncertain
                return await original(method, target)

            export = "/v1/calendar/export.ics"
            with patch.object(self.gateway, "tool", schedule):
                ics = await self.client.get(export, headers=alice)
                self.assertEqual(ics.status_code, 200, ics.text)
                self.assertEqual(ics.text.count("CATEGORIES:教室预约"), 2)
                self.assertIn("CATEGORIES:自定义事件", ics.text)
                self.assertNotIn(contact["phone"], ics.text)
                with patch.object(self.gateway, "request", offline):
                    degraded = (await self.client.get(week, headers=alice)).json()
                    self.assertTrue(degraded["classroom_unavailable"])
                    self.assertEqual({item["id"] for item in degraded["items"]},
                                     {event_id, booking.json()["id"], adjacent.json()["id"]})
                    self.assertEqual((await self.client.get(export, headers=alice)).status_code, 503)
            cancelled = await self.client.delete(f"/v1/minta/operations/{booking.json()['id']}", headers=alice)
            self.assertEqual(cancelled.json()["state"], "closed", cancelled.text)
            refreshed = (await self.client.get(week, headers=alice)).json()["items"]
            self.assertEqual({item["id"] for item in refreshed}, {event_id, adjacent.json()["id"]})

        deleted = await self.client.delete(f"{item_path}?revision=2", headers=alice)
        self.assertEqual(deleted.json(), {"deleted": True}, deleted.text)
        final = (await self.client.get(week, headers=alice)).json()["items"]
        self.assertEqual([item["kind"] for item in final], ["classroom"])

    async def test_calendar_subscription_keeps_ownership_and_tracks_schedule_changes(self):
        subscriptions = "/v1/calendar/subscriptions"
        feed_path = "/v1/calendar/feed.ics"
        export = "/v1/calendar/export.ics"
        alice, _ = await self.account("alice")
        bob, _ = await self.account("bob")
        event = {"id": "campus-event", "name": "校园,活动;试验\n" + "中文" * 18, "description": "内部资料",
                 "starts_at": "2026-11-15T16:30", "ends_at": "2026-11-15T18:00",
                 "location": "活动中心", "capacity": 5, "remaining": 5}
        lesson = {"course_id": "C101", "course_name": "高等数学", "day": "周一", "start_period": 1,
                  "end_period": 2, "classroom": "博学楼 A101", "weeks": [1, 3, 3]}
        periods = [{"period": 1, "start_time": "08:00", "end_time": "08:45"},
                   {"period": 2, "start_time": "08:55", "end_time": "09:40"}]
        original_tool, original_request = self.gateway.tool, self.gateway.request

        async def tool(service, name, arguments):
            if service == "schedule":
                self.assertEqual(arguments, {"student_id": "student-test-a"})
                return {"student_id": "private", "courses": [lesson, {**lesson, "weeks": [3]}], "periods": periods}
            if name == "club_list_activities":
                return {"activities": [event]}
            return await original_tool(service, name, arguments)

        async def request(method, target, data=None):
            if target == "/club/managed/registrations" and method == "POST":
                return {"registrations": [row for row in self.gateway.records["club"].values()
                                          if row["participant_name"] in data["participants"]]}
            if target == "/schedule/api/teaching/t002":
                return {"courses": [{**lesson, "course_name": "本人授课"}], "periods": periods}
            return await original_request(method, target)

        def auth(item):
            secret = item["path"].split("/")[-1].removesuffix(".ics")
            return {"Authorization": f"Bearer {secret}"}, secret

        def activity_uid(response):
            text = response.text.replace("\r\n ", "")
            return next(block.split("UID:", 1)[1].split("\r\n")[0]
                        for block in text.split("BEGIN:VEVENT\r\n")[1:] if "CATEGORIES:校园活动" in block)

        with patch.object(self.gateway, "tool", tool), patch.object(self.gateway, "request", request):
            self.assertEqual((await self.client.get(export)).status_code, 401)
            self.assertEqual((await self.client.post(subscriptions, json={"label": "Windows"})).status_code, 401)
            self.assertEqual((await self.client.get(feed_path, headers=alice)).status_code, 404)
            item = await self.client.post(subscriptions, headers=alice, json={"label": "Windows"})
            self.assertEqual(item.status_code, 201, item.text)
            item = item.json()
            token_headers, secret = auth(item)
            stored = await self.db.run(lambda c: c.execute(
                "SELECT token_hash FROM calendar_subscriptions WHERE id=%s", (item["id"],),
            ).fetchone())
            self.assertEqual(stored["token_hash"], digest(secret))
            current = (await self.client.get(subscriptions, headers=alice)).json()["items"]
            self.assertEqual(current, [{key: value for key, value in item.items() if key != "path"}])
            self.assertEqual((await self.client.get(subscriptions, headers=bob)).json()["items"], [])
            revoke = f"{subscriptions}/{item['id']}"
            self.assertEqual((await self.client.delete(revoke, headers=bob)).status_code, 204)
            self.assertEqual((await self.client.get("/v1/me", headers=token_headers)).status_code, 401)

            signup = await self.start(alice, "club", event["id"])
            self.assertEqual(signup.status_code, 200, signup.text)
            initial = await self.client.get(feed_path, headers=token_headers)
            self.assertEqual(initial.status_code, 200, initial.text)
            self.assertEqual(initial.headers["cache-control"], "no-store")
            self.assertTrue(initial.headers["content-type"].startswith("text/calendar"))
            self.assertIn(b"DTSTART:20260907T000000Z\r\n", initial.content)
            self.assertIn(b"DTSTART:20260921T000000Z\r\n", initial.content)
            self.assertNotIn(b"DTSTART:20260914", initial.content)
            self.assertIn(b"DTEND:20260907T014000Z\r\n", initial.content)
            self.assertIn(b"DTSTART:20261115T083000Z\r\n", initial.content)
            self.assertIn(b"DTEND:20261115T100000Z\r\n", initial.content)
            self.assertEqual(initial.content.count(b"BEGIN:VEVENT"), 3)
            self.assertTrue(all(len(row) <= 75 for row in initial.content.split(b"\r\n")))
            self.assertIn("SUMMARY:校园\\,活动\\;试验\\n中文", initial.text.replace("\r\n ", ""))
            self.assertNotIn("内部资料", initial.text)
            self.assertNotIn("participant", initial.text)
            self.assertNotIn(secret, initial.text)

            head = await self.client.head(feed_path, headers=token_headers)
            self.assertEqual((head.status_code, head.content, head.headers["content-length"]),
                             (200, b"", str(len(initial.content))))
            cached = {**token_headers, "If-None-Match": initial.headers["etag"]}
            self.assertEqual((await self.client.get(feed_path, headers=cached)).status_code, 304)
            snapshot = await self.client.get(export, headers=bob)
            self.assertNotIn("校园\\,活动", snapshot.text)
            self.assertIn("attachment", snapshot.headers["content-disposition"])
            self.assertNotIn(activity_uid(initial), snapshot.text)

            event.update(starts_at="2026-11-16T19:30+08:00", ends_at="2026-11-16T21:00+08:00")
            updated = await self.client.get(feed_path, headers=cached)
            self.assertIn("DTSTART:20261116T113000Z", updated.text)
            self.assertEqual(activity_uid(initial), activity_uid(updated))
            self.assertNotEqual(initial.headers["etag"], updated.headers["etag"])
            removed = await self.client.delete(f"/v1/minta/operations/{signup.json()['id']}", headers=alice)
            self.assertEqual(removed.json()["state"], "closed")
            self.assertNotIn("校园\\,活动", (await self.client.get(feed_path, headers=token_headers)).text)
            with patch.object(self.gateway, "tool", side_effect=GatewayUncertain):
                unavailable = await self.client.get(feed_path, headers=cached)
            self.assertEqual(unavailable.status_code, 503)
            await asyncio.to_thread(set_user_role, self.admin_dsn, "alice@gmail.com", "teacher", "讲师")
            with patch.object(self.app.state.campus_access, "schedule_number", return_value="t002"):
                teaching = await self.client.get(feed_path, headers=token_headers)
            self.assertIn("SUMMARY:本人授课", teaching.text)
            self.assertIn("CATEGORIES:授课", teaching.text)
            self.assertNotIn("高等数学", teaching.text)
            self.assertEqual((await self.client.post("/v1/auth/logout", headers=alice)).status_code, 204)
            self.assertEqual((await self.client.get(feed_path, headers=token_headers)).status_code, 200)
            signed_back_in = await self.client.post("/v1/auth/login", json={
                "email": "alice@gmail.com", "password": test_cloud.TEST_PASSWORD,
            })
            self.assertEqual(signed_back_in.status_code, 200)
            alice = {"Authorization": "Bearer " + signed_back_in.json()["access_token"]}
            self.assertEqual((await self.client.delete(revoke, headers=alice)).status_code, 204)
            self.assertEqual((await self.client.get(feed_path, headers=cached)).status_code, 404)

    async def test_club_archive_cleanup_uses_the_authenticated_actor_and_confirmation_snapshot(self):
        owner, _ = await self.account()
        actor_id = (await self.client.get("/v1/me", headers=owner)).json()["id"]
        headers = {**owner, "Idempotency-Key": "archive-cleanup"}
        path = "/v1/minta/clubs"
        counts = {"club_count": 1, "activity_count": 2}
        summary = {**counts, "snapshot": "a" * 64}
        body = {"action": "purge_archives", "snapshot": summary["snapshot"]}
        with patch.object(self.gateway, "request", side_effect=[
            {"clubs": [], "archive_summary": summary}, counts, GatewayRejected("CONFLICT", 409),
        ]) as gateway:
            preview = await self.client.get(path, headers=owner)
            self.assertEqual(preview.json()["archive_summary"], summary)
            self.assertEqual((await self.client.post(path, json=body)).status_code, 401)
            self.assertEqual((await self.client.post(path, headers=headers, json={**body, "actor_id": str(uuid4())})).status_code, 422)
            gateway.assert_awaited_once_with("GET", f"/club/managed/clubs?actor_id={actor_id}")
            response = await self.client.post(path, headers=headers, json=body)
            self.assertEqual((response.status_code, response.json()), (200, counts))
            gateway.assert_awaited_with("POST", "/club/managed/archives/purge", {
                "actor_id": actor_id, "snapshot": summary["snapshot"],
            })
            stale = await self.client.post(path, headers=headers, json=body)
            self.assertEqual((stale.status_code, stale.json()["error"]["code"]), (409, "club_archives_changed"))

    async def test_club_archive_actions_use_verified_account_and_reject_forged_actors(self):
        owner, _ = await self.account()
        actor_id = (await self.client.get("/v1/me", headers=owner)).json()["id"]
        headers = {**owner, "Idempotency-Key": "archive-action"}
        path = "/v1/minta/clubs"
        club = {"id": str(uuid4()), "version": 4, "name": "社团", "category": "艺术",
                "description": "介绍", "registrant": "登记人", "created_at": "2026-11-10T00:00:00Z",
                "archived": False, "can_manage": True, "activities": [], "review_status": "pending"}
        restore = {"action": "restore", "id": club["id"], "version": 3}
        purge = {"action": "purge_archive", "id": club["id"], "version": 3, "activity_id": str(uuid4())}
        with patch.object(self.gateway, "request", side_effect=[
            {"club": club}, {"club_count": 0, "activity_count": 1}, GatewayRejected("CLUB_REVIEW_REQUIRED", 403),
        ]) as gateway:
            reply = await self.client.post(path, headers=headers, json=restore)
            self.assertEqual((reply.status_code, reply.json()["club"]["review_status"]), (200, "pending"))
            gateway.assert_awaited_once_with("POST", "/club/managed/clubs", {**restore, "actor_id": actor_id})
            self.assertEqual((await self.client.post(path, headers=headers, json={**purge, "actor_id": str(uuid4())})).status_code, 422)
            reply = await self.client.post(path, headers=headers, json=purge)
            self.assertEqual((reply.status_code, reply.json()), (200, {"club_count": 0, "activity_count": 1}))
            gateway.assert_awaited_with("POST", f"/club/managed/archives/{club['id']}/purge", {
                "version": 3, "activity_id": purge["activity_id"], "actor_id": actor_id,
            })
            publication = {
                "action": "publish_activity", "id": club["id"], "name": "活动", "description": "介绍",
                "location": "校园", "starts_at": "2026-11-11T13:00", "ends_at": "2026-11-11T15:00", "capacity": 6,
            }
            for end in (None, "2026-11-11T05:00Z", "2026-11-11T12:59+08:00", "2026-11-11"):
                invalid = await self.client.post(path, headers=headers, json={**publication, "ends_at": end})
                self.assertEqual(invalid.status_code, 422, invalid.text)
            published = await self.client.post(path, headers=headers, json=publication)
            self.assertEqual((published.status_code, published.json()["error"]["code"]), (403, "club_review_required"))

    async def test_chat_restores_archived_club_and_tracks_undo_without_publishing_unapproved_activities(self):
        owner, _ = await self.account()
        _, conversation = await self.conversation(owner)
        actor = (await self.client.get("/v1/me", headers=owner)).json()["id"]
        club = {"id": str(uuid4()), "version": 2, "name": "摄影社", "category": "艺术",
                "description": "介绍", "registrant": "登记人", "created_at": "2026-11-10T00:00:00Z",
                "archived": True, "can_manage": True, "activities": [], "review_status": "pending"}
        original = self.gateway.request
        writes = []

        async def request(method, path, payload=None):
            if path == "/club/managed/capabilities":
                return {"actions": ["restore", "publish_activity"]}
            if path == f"/club/managed/clubs?actor_id={actor}":
                return {"clubs": [club]}
            if (method, path) == ("POST", "/club/managed/clubs"):
                writes.append(payload)
                club.update(archived=False, version=3)
                return {"club": club, "effect": "applied"}
            return await original(method, path)

        worker = WorkerPool(self.store, CampusRunner(campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth)),
                            self.config)

        async def ask(query):
            queued = await self.submit(owner, conversation, query=query)
            self.assertEqual(queued.status_code, 202, queued.text)
            await worker.process(await self.store.claim())
            return (await self.client.get(f"/v1/runs/{queued.json()['id']}", headers=owner)).json()

        with patch.object(self.gateway, "request", side_effect=request):
            result = await ask("恢复社团摄影社")
            self.assertEqual(result["result_status"], "completed", result)
            self.assertEqual(len(writes), 1)
            self.assertEqual({key: writes[0][key] for key in ("action", "id", "version", "actor_id")},
                             {"action": "restore", "id": club["id"], "version": 2, "actor_id": actor})
            plan = (await self.client.get(f"/v1/runs/{result['id']}/withdrawal", headers=owner)).json()
            self.assertEqual([item["label"] for item in plan["operations"]], ["重新归档社团「摄影社」"])
            rejected = await ask("发布社团活动摄影社，活动名称：外拍，简介：活动介绍，地点：校园，时间：2026-12-01T14:00，名额：8")
            self.assertEqual(rejected["result_status"], "needs_input", rejected)
            self.assertIn("通过审核", rejected["steps"][0]["result"]["content"])
            self.assertEqual(len(writes), 1)

    async def test_deleted_club_activity_reconciles_an_unconfirmed_registration(self):
        student, _ = await self.account()
        removed, unknown, kept = (str(uuid4()) for _ in range(3))
        for activity in (removed, unknown):
            self.gateway.lose_reply = True
            result = await self.start(student, "club", activity)
            self.assertEqual((result.status_code, result.json()["state"]), (202, "uncertain"))
        self.gateway.records["club"].clear()
        result = await self.start(student, "club", kept)
        self.assertEqual((result.status_code, result.json()["state"]), (200, "active"))
        request = self.gateway.request

        async def receipts(method, path):
            target = urlsplit(path)
            ids = parse_qs(target.query)["check_archives"][0].split(",")
            self.assertCountEqual(ids, [removed, unknown])
            return {**await request(method, target.path), "purged_activity_ids": [removed]}

        with patch.object(self.gateway, "request", side_effect=receipts):
            reply = await self.client.get("/v1/minta/operations?service=club", headers=student)
        self.assertEqual(reply.status_code, 200, reply.text)
        states = {row["resource_id"]: row["state"] for row in reply.json()["items"]}
        self.assertEqual(states, {removed: "closed", unknown: "uncertain", kept: "active"})

    async def test_withdrawal_checks_every_step_before_compensating_in_reverse_order(self):
        student, _ = await self.account()
        _, conversation = await self.conversation(student)
        worker = WorkerPool(self.store, CampusRunner(campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth)),
                            self.config)
        self.gateway.lose_reply = True
        borrowed = (await self.submit(student, conversation, query="帮我借《馆藏》")).json()
        await worker.process(await self.store.claim())
        reservation = (await self.start(student, "gym", "strength")).json()
        # Simulate a turn with two independently executed operations using the real ledger and receipts.
        await self.db.run(lambda c: c.execute(
            """INSERT INTO chat_turn_effects(run_id,kind,label,operation_id,state)
               VALUES(%s,'start','取消体育馆预约',%s,'applied')""",
            (UUID(borrowed["id"]), UUID(reservation["id"])),
        ))
        path = f"/v1/runs/{borrowed['id']}/withdrawal"
        plan = await self.client.get(path, headers=student)
        self.assertEqual(plan.status_code, 200, plan.text)
        self.assertEqual([row["label"] for row in plan.json()["operations"]], ["取消体育馆预约", "归还「馆藏」"])
        approvals = [row["id"] for row in plan.json()["operations"]]
        for invalid in (approvals[:1], approvals[::-1]):
            reply = await self.client.post(path, headers=student, json={"attempt": 1, "approvals": invalid})
            self.assertEqual((reply.status_code, reply.json()["error"]["code"]), (409, "withdrawal_plan_changed"))
            self.assertEqual(len(self.gateway.writes), 2)
        reply = await self.client.post(path, headers=student, json={"attempt": 1, "approvals": approvals})
        self.assertEqual(reply.status_code, 204, reply.text)
        self.assertEqual([row[:2] for row in self.gateway.writes[2:]],
                         [("gym", OPERATIONS["gym"].close), ("library", OPERATIONS["library"].close)])
        self.assertFalse(self.gateway.records["library"])
        self.assertFalse(self.gateway.records["gym"])

    async def test_withdraw_return_then_borrow_follows_restored_record_without_retrying_write(self):
        student, _ = await self.account()
        _, conversation = await self.conversation(student)
        worker = WorkerPool(self.store, CampusRunner(campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth)),
                            self.config)

        async def run(query):
            run_id = (await self.submit(student, conversation, query=query)).json()["id"]
            await worker.process(await self.store.claim())
            return run_id

        async def withdraw(run_id, attempt):
            path = f"/v1/runs/{run_id}/withdrawal"
            preview = await self.client.get(path, headers=student)
            self.assertEqual(preview.status_code, 200, preview.text)
            operations = preview.json()["operations"]
            self.assertEqual(len(operations), 1)
            reply = await self.client.post(path, headers=student,
                                           json={"attempt": attempt, "approvals": [operations[0]["id"]]})
            self.assertEqual(reply.status_code, 204, reply.text)

        borrow = await run("帮我借《馆藏》")
        retry = await self.client.post(f"/v1/runs/{borrow}/retry", json={"attempt": 1},
                                       headers={**student, "Idempotency-Key": str(uuid4())})
        self.assertEqual(retry.status_code, 202, retry.text)
        await worker.process(await self.store.claim())
        self.assertEqual(len(self.gateway.writes), 1)
        returned = await run("请帮我归还《馆藏》")
        self.assertFalse(self.gateway.records["library"])
        await withdraw(returned, 1)
        self.assertEqual(len(self.gateway.records["library"]), 1)
        await withdraw(borrow, 2)
        self.assertFalse(self.gateway.records["library"])
        messages = await self.client.get(f"/v1/conversations/{conversation['id']}/messages", headers=student)
        self.assertEqual(messages.json()["items"], [])

    async def test_chat_and_apps_share_operations_and_chat_requires_the_originating_session(self):
        student, _ = await self.account()
        other, _ = await self.account("bob")
        _, conversation = await self.conversation(student)
        runner = CampusRunner(campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth))
        worker = WorkerPool(self.store, runner, self.config)

        async def ask(query):
            result = await self.submit(student, conversation, query=query)
            self.assertEqual(result.status_code, 202, result.text)
            await worker.process(await self.store.claim())
            reply = await self.client.get(f"/v1/runs/{result.json()['id']}", headers=student)
            return reply.json()

        result = await ask("帮我借《馆藏》五天")
        self.assertEqual(result["result_status"], "completed", result)
        self.assertEqual(result["steps"][0]["agent_id"], "campus_services")
        self.assertIn("已为你借阅", result["steps"][0]["result"]["content"])
        self.assertIn("借期 5 天", result["steps"][0]["result"]["content"])
        path = "/v1/minta/operations?service=library"
        self.assertEqual((await self.client.get(path, headers=student)).json()["items"][0]["state"], "active")
        self.assertEqual((await self.client.get(path, headers=other)).json(), {"items": []})
        self.assertEqual((await ask("可以归还《馆藏》吗？"))["result_status"], "needs_input")
        self.assertEqual(len(self.gateway.writes), 1)
        self.assertEqual((await ask("请帮我归还《馆藏》"))["result_status"], "completed")
        self.assertEqual((await self.client.get(path, headers=student)).json()["items"][0]["state"], "closed")

        queued = await self.submit(student, conversation, query="借阅《馆藏》")
        await self.client.post("/v1/auth/logout", headers=student)
        await worker.process(await self.store.claim())
        run_id = UUID(queued.json()["id"])
        saved = await self.db.run(lambda c: c.execute("SELECT result_status FROM runs WHERE id=%s", (run_id,)).fetchone())
        self.assertEqual(saved["result_status"], "needs_input")
        self.assertEqual(len(self.gateway.writes), 2)

    async def test_library_anonymous_stock_and_ownership_and_same_gym_flow_for_roles(self):
        student, _ = await self.account()
        teacher, _ = await self.account("teacher")
        await asyncio.to_thread(set_user_role, self.admin_dsn, "teacher@gmail.com", "teacher", "教授")
        catalog = "/v1/minta/catalog/library"
        self.assertEqual((await self.client.get(catalog)).status_code, 401)
        for headers in (student, teacher):
            gym = (await self.start(headers, "gym", "strength")).json()
            self.assertEqual(gym["state"], "active", gym)
            cancelled = await self.client.delete(f"/v1/minta/operations/{gym['id']}", headers=headers)
            self.assertEqual(cancelled.json()["state"], "closed")

        for value in (0, 15, 1.5, True, "7"):
            invalid = await self.start(student, "library", "book-one", duration_days=value)
            self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual((await self.start(student, "gym", "strength", duration_days=2)).status_code, 422)
        self.assertFalse(self.gateway.records["library"])

        self.gateway.lose_reply = True
        borrow = await self.start(student, "library", "book-one", key="borrow-once", duration_days=14)
        self.assertEqual(borrow.status_code, 202, borrow.text)
        operation_id = borrow.json()["id"]
        delete = f"/v1/minta/operations/{operation_id}"
        self.assertEqual((await self.client.delete(delete, headers=teacher)).status_code, 404)
        path = "/v1/minta/operations?service=library"
        self.assertEqual((await self.client.get(path, headers=teacher)).json(), {"items": []})
        stock = (await self.client.get(catalog, headers=teacher)).json()["items"][0]
        periods = stock.pop("loan_progress")
        self.assertEqual(stock, {"id": "book-one", "title": "馆藏", "author": "作者", "total_copies": 2,
                                 "category": "文学与小说", "category_id": "literature",
                                 "available_copies": 1, "borrowed_copies": 1})
        self.assertEqual(len(periods), 1)
        window = periods[0]
        self.assertEqual(set(window), {"borrowed_at", "due_at", "duration_days"})
        self.assertEqual(window["duration_days"], 14)
        self.assertEqual(datetime.fromisoformat(window["due_at"]) - datetime.fromisoformat(window["borrowed_at"]),
                         timedelta(days=14))
        records = (await self.client.get(path, headers=student)).json()["items"]
        self.assertEqual((records[0]["id"], records[0]["state"]), (operation_id, "active"))
        self.assertNotIn("borrower_name", records[0])
        self.assertNotIn("upstream_id", records[0])
        self.assertEqual(datetime.fromisoformat(records[0]["due_at"]), datetime.fromisoformat(window["due_at"]))
        self.assertEqual((await self.start(student, "library", "book-one", key="borrow-once", duration_days=14)).json(),
                         records[0])
        self.assertEqual((await self.start(student, "library", "book-one", key="borrow-once", duration_days=1)).status_code, 409)
        self.assertEqual((await self.start(student, "library", "other", key="borrow-once", duration_days=14)).status_code, 409)
        self.assertEqual((await self.start(student, "library", "book-one")).status_code, 409)
        self.assertEqual(len(self.gateway.records["library"]), 1)
        other = (await self.start(teacher, "library", "book-one", duration_days=1)).json()
        self.assertEqual(other["state"], "active", other)
        full = (await self.client.get(catalog, headers=student)).json()["items"][0]
        self.assertEqual(full["available_copies"], 0)
        self.assertCountEqual([entry["duration_days"] for entry in full["loan_progress"]], [1, 14])

        self.gateway.lose_reply = True
        self.assertEqual((await self.client.delete(delete, headers=student)).status_code, 202)
        self.assertEqual((await self.client.get(path, headers=student)).json()["items"][0]["state"], "closed")
        self.assertEqual((await self.client.delete(delete, headers=student)).json()["state"], "closed")
        updated = (await self.client.get(catalog, headers=student)).json()["items"][0]
        self.assertEqual(updated["available_copies"], 1)
        self.assertEqual([entry["duration_days"] for entry in updated["loan_progress"]], [1])
        self.assertEqual(len([write for write in self.gateway.writes if write[0] == "library"]), 3)

    async def test_gym_slots_require_confirmation_and_allow_multiple_appointments_at_one_venue(self):
        headers, _ = await self.account()
        slots = await self.client.get("/v1/minta/catalog/gym?venue_id=strength&date=2026-11-14", headers=headers)
        self.assertEqual(slots.status_code, 200, slots.text)
        first, second = slots.json()["items"]
        self.assertEqual((await self.start(headers, "gym", "strength", identity_confirmed=False)).status_code, 422)
        self.assertFalse(self.gateway.writes)
        booked = await self.start(headers, "gym", "strength", slot_id=first["slot_id"], key="same-slot")
        self.assertEqual(booked.json()["state"], "active", booked.text)
        self.assertEqual(booked.json()["court_name"], first["court_name"])
        repeat = await self.start(headers, "gym", "strength", slot_id=first["slot_id"], key="same-slot")
        self.assertEqual(repeat.json()["id"], booked.json()["id"])
        duplicate = await self.start(headers, "gym", "strength", slot_id=first["slot_id"])
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        later = await self.start(headers, "gym", "strength", slot_id=second["slot_id"])
        self.assertEqual(later.json()["state"], "active", later.text)
        self.assertNotIn("user_name", later.json())
        self.assertEqual(len(self.gateway.writes), 2)

    async def test_gym_chat_keeps_previous_details_and_waits_for_confirmation(self):
        headers, _ = await self.account()
        _, conversation = await self.conversation(headers)
        _, unrelated = await self.conversation(headers, "另一实例")
        first = "帮我预约室内篮球场"
        when = "明天上午九点"
        court = "一号场就行"
        change = "改成二号场上午十点"
        day = (datetime.now(UTC) + timedelta(hours=8, days=1)).date().isoformat()
        plan = {"action": "gym.start", "target": "indoor-basketball", "arguments": {}}
        changes = {when: {"date": day, "start_time": "09:00"}, court: {"court_id": "1"},
                   change: {"court_id": "2", "start_time": "10:00"}}
        reviews = []
        gateway_request = self.gateway.request

        async def catalog(method, path):
            if path == "/gym/api/venues":
                return {"venues": [{"id": "indoor-basketball", "name": "室内篮球场", "capacity": 2,
                                    "remaining": 2, "court_count": 2}]}
            return await gateway_request(method, path)

        async def complete(messages, tools):
            self.assertEqual(tools, [])
            system = messages[0].content
            payload = json.loads(messages[-1].content)
            if system.startswith("你是 MainAgent。"):
                return Message("assistant", '{"decision":"other"}')
            if "范围判断" in system:
                return Message("assistant", '{"campus":true}')
            if system.startswith("你负责校园智能体路由。"):
                self.assertIn("gym.start", system)
                self.assertIn("campus_today", payload)
                if payload["query"] == first:
                    return Message("assistant", json.dumps({"campus_action": plan}, ensure_ascii=False))
                if "campus_action_draft" not in payload:
                    self.assertNotIn(first, [item["content"] for item in payload["history"]])
                    return Message("assistant", '{"clarification":"请告诉我是哪项预约。"}')
                self.assertIn(first, [item["content"] for item in payload["history"]])
                return Message("assistant", json.dumps({"continue_campus_action": {"arguments": changes[payload["query"]]}}))
            self.assertEqual([rule["id"] for rule in payload["rules"]], ["campus.action_plan"])
            reviews.append(json.loads(payload["answer"]))
            return action_review("pass")

        async def local_capabilities(registrations):
            return registrations

        runner = CampusRunner.with_model(SimpleNamespace(complete=complete),
                                         campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth))
        worker = WorkerPool(self.store, runner, self.config)

        async def ask(chat, query):
            sent = await self.submit(headers, chat, query=query)
            self.assertEqual(sent.status_code, 202, sent.text)
            await worker.process(await self.store.claim())
            return (await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=headers)).json()

        with (patch("main_agent.cloud.runner.load_subagent_capabilities", local_capabilities),
              patch.object(self.gateway, "request", catalog)):
            details = await ask(conversation, first)
            self.assertIn("室内篮球场", details["messages"][-1]["content"])
            self.assertIn("请补充日期、开始时间、场地号", details["messages"][-1]["content"])
            self.assertEqual(details["steps"][0]["result"]["campus_action_draft"]["target"], "室内篮球场")
            self.assertIn("是哪项预约", (await ask(unrelated, when))["messages"][-1]["content"])
            incomplete = await ask(conversation, "确认预约")
            self.assertIn("请补充日期", incomplete["messages"][-1]["content"])
            details = await ask(conversation, when)
            self.assertIn("请补充场地号", details["messages"][-1]["content"])
            self.assertFalse(reviews)
            self.assertFalse(self.gateway.writes)
            preview = await ask(conversation, court)
            self.assertIn("预约人", preview["messages"][-1]["content"])
            self.assertIn("09:00–10:00", preview["messages"][-1]["content"])
            amended = await ask(conversation, change)
            self.assertIn("10:00–11:00", amended["messages"][-1]["content"])
            self.assertEqual(len(reviews), 2)
            self.assertEqual((await ask(unrelated, "确认预约"))["result_status"], "needs_input")
            self.assertFalse(self.gateway.writes)
            completed = await ask(conversation, "确认预约")
            self.assertEqual(completed["result_status"], "completed", completed)
            self.assertEqual(self.gateway.writes[0][2]["slot_id"], f"{day}_2_1000")
            self.assertEqual(len(self.gateway.writes), 1)

    async def test_gym_chat_can_book_a_slot_from_its_own_timetable(self):
        headers, _ = await self.account()
        instance, conversation = await self.conversation(headers)
        other = (await self.client.post(f"/v1/instances/{instance['id']}/conversations", json={}, headers=headers)).json()
        day = (datetime.now(UTC) + timedelta(hours=8, days=1)).date().isoformat()
        lookup = "现在有乒乓球馆吗"
        selection = "我要预定4号桌的9点到10点"
        court_only = "我要预定4号桌"
        wrong_range = f"我要预约{day}室内乒乓球馆4号桌的9点到11点"
        original = self.gateway.request

        async def gateway_request(method, path):
            if path == "/gym/api/venues":
                return {"venues": [{"id": "table-tennis", "name": "室内乒乓球馆", "capacity": 4,
                                    "remaining": 4, "court_count": 4}],
                        "booking_window": {"first_date": day, "last_date": day}}
            if urlsplit(path).path == "/gym/api/slots":
                slots = [self.gateway.slot(day, court, clock)
                         for court, clock in (("2", "0900"), ("4", "0900"), ("4", "1000"))]
                return {"slots": [{**slot, "court_name": slot["court_id"] + " 号桌"} for slot in slots]}
            return await original(method, path)

        route_inputs = []

        async def complete(messages, tools):
            self.assertEqual(tools, [])
            system = messages[0].content
            payload = json.loads(messages[-1].content)
            if system.startswith("你是 MainAgent。"):
                return Message("assistant", '{"decision":"other"}')
            if "范围判断" in system:
                return Message("assistant", '{"campus":true}')
            if system.startswith("你负责校园智能体路由。"):
                route_inputs.append(payload)
                if payload["query"] == lookup:
                    route = {"timetable_query": {"service": "gym", "target": "乒乓球馆", "date": day}}
                else:
                    self.assertIn(payload["query"], (selection, court_only, wrong_range))
                    arguments = {"court_id": "4"}
                    if payload["query"] != court_only:
                        arguments.update(start_time="09:00", end_time="10:00")
                    target = "table-tennis"
                    if payload["query"] == wrong_range:
                        target = "室内乒乓球馆"
                        arguments.update(date=day, end_time="11:00")
                    route = {"campus_action": {"action": "gym.start", "target": target, "arguments": arguments}}
                return Message("assistant", json.dumps(route, ensure_ascii=False))
            self.assertEqual([rule["id"] for rule in payload["rules"]], ["campus.action_plan"])
            proposal = json.loads(payload["answer"])
            if proposal["target"] == "table-tennis":
                slot = json.loads(payload["evidence"][0]["content"])
                self.assertEqual((slot["venue_id"], slot["date"], slot["court_id"]), ("table-tennis", day, "4"))
            return action_review("pass")

        async def local_capabilities(registrations):
            return registrations

        runner = CampusRunner.with_model(SimpleNamespace(complete=complete),
                                         campus_chat=CampusChat(self.app.state.campus_ops, self.app.state.auth))
        worker = WorkerPool(self.store, runner, self.config)

        async def ask(chat, query):
            sent = await self.submit(headers, chat, query=query)
            self.assertEqual(sent.status_code, 202, sent.text)
            await worker.process(await self.store.claim())
            return (await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=headers)).json()

        with (patch.object(self.gateway, "request", gateway_request),
              patch("main_agent.cloud.runner.load_subagent_capabilities", local_capabilities)):
            table = await ask(conversation, lookup)
            self.assertEqual(table["result_status"], "completed", table)
            self.assertIn("室内乒乓球馆 · 4 号桌", table["messages"][-1]["content"])
            self.assertEqual((await ask(other, selection))["result_status"], "needs_input")
            self.assertNotIn("recent_gym_slots", route_inputs[-1])
            ambiguous = await ask(conversation, court_only)
            self.assertIn("请补充时段", ambiguous["messages"][-1]["content"])
            mismatch = await ask(conversation, wrong_range)
            self.assertIn("到 10:00 结束", mismatch["messages"][-1]["content"])
            preview = await ask(conversation, selection)
            self.assertEqual(len(route_inputs[-1]["recent_gym_slots"]), 3)
            self.assertIn(f"室内乒乓球馆 · 4 号桌 · {day} 09:00–10:00", preview["messages"][-1]["content"])
            self.assertIn("确认预约", preview["messages"][-1]["content"])
            self.assertFalse(self.gateway.writes)
            completed = await ask(conversation, "确认预约")
            self.assertEqual(completed["result_status"], "completed", completed)
            self.assertEqual([args["slot_id"] for _, _, args in self.gateway.writes], [f"{day}_4_0900"])

    async def test_library_search_and_account_loans_are_available_in_the_bound_skill(self):
        self.gateway.books.extend(
            {"id": f"book-{index}", "title": f"Python 编程 {index}", "author": "Eric Matthes",
             "category": "计算机与技术", "category_id": "computing", "total_copies": 3}
            for index in range(1, 10)
        )
        owner, _ = await self.account()
        other, _ = await self.account("bob")
        path = "/v1/minta/catalog/library"
        filters = {"q": "ＰＹＴＨＯＮ Matthes", "category": "计算机与技术", "page": 2}
        second_page = (await self.client.get(path, params=filters, headers=other)).json()
        self.assertEqual((second_page["total"], second_page["page"]), (9, 2))
        self.assertEqual([item["id"] for item in second_page["items"]], ["book-9"])
        self.assertEqual({category["id"] for category in second_page["categories"]}, {"literature", "computing"})
        self.assertEqual((await self.client.get(path, params={"page": 0}, headers=other)).status_code, 422)
        self.assertEqual((await self.client.get(path + "?q=x&q=y", headers=other)).status_code, 422)
        self.assertEqual((await self.client.get(path, params={"category": "arts"}, headers=other)).json()["items"], [])

        started = await self.start(owner, "library", "book-9", duration_days=5)
        self.assertEqual(started.status_code, 200, started.text)
        operation = started.json()
        campus = CampusChat(self.app.state.campus_ops, self.app.state.auth)
        agent = CampusServicesAgent()

        async def tools_for(headers):
            _, conversation = await self.conversation(headers)
            sent = await self.submit(headers, conversation, query="查看图书馆馆藏和借阅")
            self.assertEqual(sent.status_code, 202)
            values = await self.store.execution_context(await self.store.claim())
            context = SimpleNamespace(**values)
            registry = agent.skill_registry.with_handlers(campus.read_handlers(context))
            return registry.bind(AgentTask(str(context.conversation_id), "test-library", agent.profile, "查书"))

        async def read(client, **options):
            result = await client.call_tool("minta_campus_status", {"service": "library", "library": options})
            self.assertEqual(result.status, "success", result.content)
            return json.loads(result.content)

        other_tools = await tools_for(other)
        found = await read(other_tools, view="catalog", q="book-9", category="computing")
        self.assertEqual([item["id"] for item in found["items"]], ["book-9"])
        self.assertEqual(found["items"][0]["available_copies"], 2)
        self.assertEqual(found["items"][0]["loan_progress"][0]["duration_days"], 5)
        self.assertNotIn("borrower_name", json.dumps(found))
        self.assertNotIn("my_operations", found)
        self.assertEqual((await read(other_tools, view="my_loans"))["my_operations"], [])

        owner_tools = await tools_for(owner)
        active = (await read(owner_tools, view="my_loans", q="Python 9"))["my_operations"]
        self.assertEqual([(item["id"], item["duration_days"], item["due_at"]) for item in active],
                         [(operation["id"], 5, operation["due_at"])])
        returned = await self.client.delete(f"/v1/minta/operations/{operation['id']}", headers=owner)
        self.assertEqual(returned.json()["state"], "closed")
        self.assertEqual((await read(owner_tools, view="my_loans"))["my_operations"][0]["state"], "closed")
        self.assertEqual((await read(other_tools, view="catalog", q="book-9"))["items"][0]["available_copies"], 3)

    async def test_classroom_credentials_link_to_verified_teacher_email_and_sso(self):
        student, _ = await self.account()
        teacher, _ = await self.account("teacher")
        teacher_id = UUID((await self.client.get("/v1/me", headers=teacher)).json()["id"])
        await asyncio.to_thread(set_user_role, self.admin_dsn, "teacher@gmail.com", "teacher", "讲师")
        _, student_chat = await self.conversation(student)
        _, teacher_chat = await self.conversation(teacher)
        campus = CampusChat(self.app.state.campus_ops, self.app.state.auth)
        worker = WorkerPool(self.store, CampusRunner(campus_chat=campus), self.config)

        async def ask(headers, chat, query):
            sent = await self.submit(headers, chat, query=query)
            self.assertEqual(sent.status_code, 202, sent.text)
            await worker.process(await self.store.claim())
            result = await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=headers)
            return result.json()

        question = "帮我预定一个教室"
        original = (await self.submit(student, student_chat, query=question)).json()
        await self.worker.process(await self.store.claim())
        path = f"/v1/runs/{original['id']}"
        retry = await self.client.post(path + "/retry", json={"attempt": 1},
                                       headers={**student, "Idempotency-Key": str(uuid4())})
        self.assertEqual(retry.status_code, 202, retry.text)
        await worker.process(await self.store.claim())
        student_prompt = (await self.client.get(path, headers=student)).json()["steps"][0]
        self.assertEqual((student_prompt["step_id"], student_prompt["status"]), ("minta-action", "needs_input"))
        self.assertTrue(student_prompt["result"]["classroom_details_pending"])
        for field in ("教室编号", "老师姓名", "电话号码"):
            self.assertIn(field, student_prompt["result"]["content"])
        teacher_prompt = (await ask(teacher, teacher_chat, question))["steps"][0]["result"]["content"]
        self.assertIn("教室编号", teacher_prompt)
        self.assertNotIn("电话号码", teacher_prompt)
        self.assertFalse(self.gateway.writes)

        with TemporaryDirectory(prefix="minta-teacher-test-") as folder:
            directory = Path(folder)
            app = create_app(self.config, directory=directory)
            async with app.router.lifespan_context(app), httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://credential-test",
            ) as credential_http:
                key = read_key(directory)
                operator = {"Authorization": f"Bearer {credential_digest(key, 'operator')}"}
                self.app.state.campus_ops.credentials = TeacherCredentialClient(
                    credential_http, url="http://credential-test", directory=directory,
                )
                contact = {"name": "测试教师", "phone": "13900001234"}
                payload = {"email": "teacher@gmail.com", "actor": "test-operator", "contact": contact}
                self.assertEqual((await credential_http.post("/manage/issue", json=payload)).status_code, 401)
                student_payload = {**payload, "email": "alice@gmail.com"}
                forbidden = await credential_http.post("/manage/issue", json=student_payload, headers=operator)
                self.assertEqual(forbidden.status_code, 422)
                self.assertEqual((await self.start(teacher, "classroom", "room-a101")).status_code, 403)
                self.assertEqual((await self.start(student, "classroom", "room-a101")).status_code, 422)

                issued = await credential_http.post("/manage/issue", json=payload, headers=operator)
                self.assertEqual(issued.status_code, 200, issued.text)
                self.assertNotIn(contact["phone"], issued.text)
                self.assertFalse(issued.json()["campus_linked"])
                denied = await self.start(student, "classroom", "room-a101", teacher={**contact, "phone": "13900004321"})
                self.assertEqual(denied.status_code, 403)
                valid = await self.start(student, "classroom", "room-a101", teacher=contact)
                self.assertEqual(valid.json()["state"], "active", valid.text)
                self.assertNotIn(contact["phone"], valid.text)
                self.assertNotIn(contact["name"], str(self.gateway.writes))
                ledger = await self.db.run(lambda connection: connection.execute(
                    "SELECT teacher_credential_id FROM campus_minta_operations WHERE id=%s", (UUID(valid.json()["id"]),),
                ).fetchone())
                self.assertEqual(str(ledger["teacher_credential_id"]), issued.json()["id"])
                period = classroom_times()
                times = f"开始时间：{period['starts_at']}，结束时间：{period['ends_at']}"
                reply = f"A103，{contact['name']}，{contact['phone']}，{times}"
                _, unrelated_chat = await self.conversation(student)
                writes = len(self.gateway.writes)
                unrelated = await ask(student, unrelated_chat, reply)
                self.assertFalse(any(step["step_id"] == "minta-action" for step in unrelated["steps"]))
                self.assertEqual(len(self.gateway.writes), writes)
                via_chat = await ask(student, student_chat, reply)
                self.assertEqual(via_chat["result_status"], "completed")
                self.assertIn("已为你预约", via_chat["steps"][0]["result"]["content"])
                self.assertEqual(self.gateway.writes[-1][2]["room_id"], "room-a103")
                self.assertNotIn(contact["phone"], str(self.gateway.writes))
                self.assertEqual(len(self.gateway.writes), writes + 1)
                chat_path = f"/v1/runs/{via_chat['id']}"
                retry = await self.client.post(chat_path + "/retry", json={"attempt": 1},
                                               headers={**student, "Idempotency-Key": str(uuid4())})
                self.assertEqual(retry.status_code, 202, retry.text)
                await worker.process(await self.store.claim())
                self.assertEqual(len(self.gateway.writes), writes + 1)
                plan = (await self.client.get(chat_path + "/withdrawal", headers=student)).json()
                self.assertEqual(len(plan["operations"]), 1)
                withdrawn = await self.client.post(chat_path + "/withdrawal", headers=student,
                                                  json={"attempt": 2, "approvals": [plan["operations"][0]["id"]]})
                self.assertEqual(withdrawn.status_code, 204, withdrawn.text)
                self.assertEqual(len(self.gateway.writes), writes + 2)

                await ask(student, student_chat, f"帮我预定 A103 教室，{times}")
                partial = await ask(student, student_chat, f"老师姓名：{contact['name']}")
                prompt = partial["steps"][0]["result"]["content"]
                self.assertIn("老师电话号码", prompt)
                self.assertNotIn("教室编号", prompt)
                self.assertNotIn("老师姓名", prompt)
                self.assertEqual(len(self.gateway.writes), writes + 2)
                done = await ask(student, student_chat, contact["phone"])
                self.assertEqual(done["result_status"], "completed", done)
                self.assertEqual(len(self.gateway.writes), writes + 3)

                await self.db.run(lambda connection: connection.execute(
                    "INSERT INTO external_identities(user_id,protocol,issuer,subject) VALUES(%s,'oidc',%s,%s)",
                    (teacher_id, "https://sso.example.test", "teacher-subject"),
                ))
                view = "/v1/minta/teacher-credential"
                self.assertEqual((await self.client.get(view, headers=teacher)).json(), {"credential": None})
                updated = await credential_http.post("/manage/update", json=payload, headers=operator)
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertNotEqual(updated.json()["id"], issued.json()["id"])
                self.assertTrue((await self.client.get(view, headers=teacher)).json()["credential"]["campus_linked"])
                booked = await ask(teacher, teacher_chat, f"A102，{times}")
                self.assertEqual(booked["result_status"], "completed", booked)
                revoked = await credential_http.post("/manage/revoke", json=payload, headers=operator)
                self.assertTrue(revoked.json()["revoked"])
                self.assertEqual((await self.start(teacher, "classroom", "room-a103")).status_code, 403)

    async def test_main_agent_can_interpret_classroom_replies_and_requests_explicit_confirmation(self):
        phone = "13900001234"
        period = classroom_times()
        time_text = f"{period['starts_at']} 到 {period['ends_at']}"
        student, _ = await self.account()
        _, conversation = await self.conversation(student)
        early_message = "会话前文" + "校务资料" * 180 + "末尾标记"
        seeded = await self.submit(student, conversation, query=early_message)
        self.assertEqual(seeded.status_code, 202)
        await self.worker.process(await self.store.claim())
        calls, confirmation_reviews = [], []

        async def complete(messages, tools):
            self.assertEqual(tools, [])
            prompt = messages[-1].content
            self.assertNotIn(phone, prompt)
            if messages[0].content.startswith("你是独立的答案评审器"):
                review = json.loads(prompt)
                candidate = json.loads(review["answer"])
                if candidate.get("decision") == "confirm":
                    self.assertEqual([rule["id"] for rule in review["rules"]], ["campus.action_confirmation"])
                    self.assertIn(early_message, [item["content"] for item in review["messages"]])
                    pending = candidate["pending"]
                    self.assertEqual(pending["plan"]["arguments"]["phone"], "[电话已隐藏]")
                    direct = pending["previous_reply_is_preview"]
                    confirmation_reviews.append((pending["plan"]["target"], direct))
                    approved = direct and review["messages"][-1]["content"] == "确认"
                    return action_review("pass" if approved else "fail", "campus.action_confirmation")
                self.fail("教室草稿由 MainAgent 直接交给校园服务，语义审核用于自然语言确认")
            request = json.loads(prompt)
            query = request["reply"]
            calls.append(request)
            if "预约教室流程" in query:
                return Message("assistant", '{"decision":"other"}')
            if "帮我预约A102" in query and "明天下午" in query:
                return Message("assistant", '{"decision":"other"}')
            if query in {"确认", "可以,但要改成明天下午"}:
                self.assertGreater(len(request["history"]), 8)
                self.assertEqual(request["history"][0]["content"], early_message)
                self.assertIn("确认预约", request["pending"]["preview"])
                return Message("assistant", '{"decision":"confirm"}')
            if query == "市调":
                self.assertTrue(request["awaiting_confirmation"])
                self.assertEqual(request["room"], "room-a101")
                self.assertTrue(any("确认预约" in item["content"] for item in request["history"]))
                return Message("assistant", '{"decision":"clarify"}')
            if query in {"可以帮我预约教室吗", "帮我预约一个教室"}:
                decision, target, arguments = "start", "", {}
            elif query == "A101":
                self.assertTrue(request["has_draft"])
                decision, target, arguments = "fill", "A101", {}
            elif query == time_text:
                self.assertTrue(request["has_draft"])
                decision, target, arguments = "fill", "", period
            elif "帮我预约A103" in query:
                self.assertTrue(request["has_draft"])
                self.assertIn("<phone-1>", query)
                decision, target = "fill", "A103"
                arguments = {"name": "测试教师", "phone": "<phone-1>", **period}
            elif "帮我预约A102" in query:
                self.assertFalse(request["has_draft"])
                self.assertIn("<phone-1>", query)
                decision, target = "start", "A102"
                arguments = {"name": "测试教师", "phone": "<phone-1>", **period}
            else:
                decision, target = "fill", ""
                arguments = {"name": "测试教师", "phone": "<phone-1>"}
            return Message("assistant", json.dumps({
                "decision": decision, "plan": {"action": "classroom.start", "target": target, "arguments": arguments},
            }, ensure_ascii=False))

        config = SimpleNamespace(llm=SimpleNamespace(complete=complete))
        campus = CampusChat(self.app.state.campus_ops, self.app.state.auth)
        worker = WorkerPool(self.store, CampusRunner(answerer=config, campus_chat=campus), self.config)

        async def ask(query):
            sent = await self.submit(student, conversation, query=query)
            self.assertEqual(sent.status_code, 202, sent.text)
            await worker.process(await self.store.claim())
            return (await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=student)).json()

        with TemporaryDirectory(prefix="minta-interpret-test-") as folder:
            directory = Path(folder)
            read_key(directory, create=True)

            async def verify(kind, contact):
                self.assertEqual((kind, contact), ("contact", {"name": "测试教师", "phone": phone}))
                return {"id": str(uuid4())}

            self.app.state.campus_ops.credentials = SimpleNamespace(directory=directory, verify=verify)
            inquiry = await ask("预约教室流程")
            self.assertFalse(any(step["step_id"] == "minta-action" for step in inquiry["steps"]))
            first = await ask("可以帮我预约教室吗")
            self.assertEqual(first["result_status"], "needs_input", first)
            self.assertTrue(first["steps"][0]["result"]["classroom_details_pending"])
            self.assertIn("教室编号", first["messages"][-1]["content"])
            second = await ask(f"我的指导老师是测试教师，联系电话是{phone}。")
            self.assertEqual(second["result_status"], "needs_input", second)
            self.assertIn("教室编号", second["steps"][0]["result"]["content"])
            unresolved = await ask("A101")
            self.assertIn("开始时间", unresolved["messages"][-1]["content"])
            proposal = await ask(time_text)
            result = proposal["steps"][0]["result"]
            self.assertTrue(result["classroom_confirmation"])
            self.assertEqual(result["classroom_target"], "room-a101")
            self.assertIn("A101", result["content"])
            self.assertIn("确认预约", result["content"])
            self.assertNotIn(phone, str(proposal["steps"]))
            self.assertEqual(len(calls), 5)
            side = await ask("市调")
            self.assertEqual(side["result_status"], "needs_input", side)
            self.assertIn("刚才的教室预约", side["messages"][-1]["content"])
            ambiguous = await ask("确认")
            self.assertIn("确认预约", ambiguous["messages"][-1]["content"])
            self.assertFalse(self.gateway.writes)
            done = await ask("确认预约")
            self.assertEqual(done["result_status"], "completed", done)
            self.assertEqual([write[2]["room_id"] for write in self.gateway.writes], ["room-a101"])
            self.assertEqual(self.gateway.writes[-1][2]["starts_at"], period["starts_at"] + ":00+08:00")
            duplicate = await ask("确认预约")
            self.assertIn("没有待确认的预约", duplicate["messages"][-1]["content"])
            self.assertEqual(len(self.gateway.writes), 1)

            pending = await ask("帮我预约一个教室")
            self.assertTrue(pending["steps"][0]["result"]["classroom_details_pending"])
            mixed = await ask(f"帮我预约A103，测试教师，{phone}，{time_text}")
            result = mixed["steps"][0]["result"]
            self.assertTrue(result["classroom_confirmation"])
            self.assertEqual(result["classroom_target"], "room-a103")
            self.assertIn("A103", result["content"])
            self.assertIn("「确认」", result["content"])
            self.assertEqual(len(calls), 9)
            self.assertEqual(len(self.gateway.writes), 1)
            done = await ask("确认")
            self.assertEqual(done["result_status"], "completed", done)
            self.assertEqual([write[2]["room_id"] for write in self.gateway.writes], ["room-a101", "room-a103"])
            self.assertEqual((await ask("确认"))["result_status"], "needs_input")

            conditional = await ask(f"帮我预约A102，测试教师，{phone}，明天下午")
            self.assertFalse(any(step["step_id"] == "minta-action" for step in conditional["steps"]))
            standalone = await ask(f"帮我预约A102，测试教师，{phone}，{time_text}")
            self.assertTrue(standalone["steps"][0]["result"]["classroom_confirmation"])
            amended = await ask("可以，但要改成明天下午")
            self.assertEqual(amended["result_status"], "needs_input", amended)
            self.assertEqual(len(calls), 13)
            self.assertEqual(confirmation_reviews, [("room-a101", False), ("room-a103", True), ("room-a102", True)])
            abandoned = await ask("放弃预约")
            self.assertEqual(abandoned["result_status"], "completed", abandoned)
            self.assertEqual((await ask("确认预约"))["result_status"], "needs_input")
            self.assertEqual(len(self.gateway.writes), 2)

    async def test_routed_action_uses_one_review_and_confirmation_in_its_own_conversation(self):
        student, _ = await self.account()
        _, conversation = await self.conversation(student)
        _, other_conversation = await self.conversation(student, "另一实例")
        planned = {"action": "library.start", "target": "馆藏", "arguments": {"duration_days": "十二天"}}
        reviews, decisions, confirmation_reviews = [], [], []

        request = "把刚才那本借给我十二天吧。"
        natural_confirmation = "按刚才展示的《馆藏》借阅提交吧。"
        followup = "解释什么是归还期限"

        async def complete(messages, tools):
            self.assertEqual(tools, [])
            system = messages[0].content
            if system.startswith("你是 MainAgent。"):
                dialogue = json.loads(messages[-1].content)
                query = dialogue["reply"]
                decisions.append(query)
                self.assertIsNotNone(dialogue["pending"]["preview"])
                if query == "确认":
                    # Even a model response that includes replacement data must not change the pending plan.
                    return Message("assistant", '{"decision":"confirm","plan":{"target":"另一册"}}')
                if query in {natural_confirmation, "确认。"}:
                    return Message("assistant", '{"decision":"confirm"}')
                return Message("assistant", '{"decision":"other"}')
            if "范围判断" in system:
                query = json.loads(messages[-1].content)["query"]
                if followup in query:
                    return Message("assistant", '{"compound":true}')
                return Message("assistant", '{"campus":true}' if query == request else '{"general":true}')
            if system.startswith("你负责校园智能体路由。"):
                self.assertIn("library.start", system)
                self.assertIn("想借的书是《馆藏》", messages[-1].content)
                if followup in json.loads(messages[-1].content)["query"]:
                    return Message("assistant", json.dumps({"parts": [
                        {"query": "归还《馆藏》", "route": {"campus_action": planned}},
                        {"query": followup, "route": {"general": True}},
                    ]}, ensure_ascii=False))
                return Message("assistant", json.dumps({"campus_action": planned}))
            if system.startswith("你是中传校园助手。结合本轮请求"):
                return Message("assistant", "归还期限是最迟还书日期。" if messages[-1].content == followup else "好的。")
            self.assertTrue(system.startswith("你是独立的答案评审器"), system)
            review = json.loads(messages[-1].content)
            candidate = json.loads(review["answer"])
            if candidate.get("decision") == "confirm":
                self.assertEqual([rule["id"] for rule in review["rules"]], ["campus.action_confirmation"])
                confirmation = candidate["pending"]
                confirmation_reviews.append((confirmation["plan"]["action"], confirmation["previous_reply_is_preview"]))
                return action_review("pass", "campus.action_confirmation")
            self.assertEqual([rule["id"] for rule in review["rules"]], ["campus.action_plan"])
            self.assertEqual(candidate, planned)
            reviews.append(planned)
            return action_review("fail" if len(reviews) == 1 else "pass")

        async def local_capabilities(registrations):
            return registrations

        campus = CampusChat(self.app.state.campus_ops, self.app.state.auth)
        runner = CampusRunner.with_model(SimpleNamespace(complete=complete), campus_chat=campus)
        worker = WorkerPool(self.store, runner, self.config)

        async def ask(chat, query):
            sent = await self.submit(student, chat, query=query)
            self.assertEqual(sent.status_code, 202, sent.text)
            await worker.process(await self.store.claim())
            return (await self.client.get(f"/v1/runs/{sent.json()['id']}", headers=student)).json()

        with patch("main_agent.cloud.runner.load_subagent_capabilities", local_capabilities):
            await ask(conversation, "想借的书是《馆藏》。")
            rejected = await ask(conversation, request)
            self.assertEqual(rejected["result_status"], "needs_input", rejected)
            self.assertEqual(rejected["steps"], [])
            self.assertEqual((await ask(conversation, "确认办理"))["result_status"], "needs_input")
            pending = await ask(conversation, request)
            self.assertEqual(pending["result_status"], "needs_input", pending)
            step = pending["steps"][0]
            self.assertEqual((step["step_id"], step["agent_id"]), ("minta-action", "campus_services"))
            self.assertEqual(step["result"]["pending_campus_action"],
                             {**planned, "target": "book-one", "arguments": {"duration_days": "12"}})
            self.assertIn("确认办理", step["result"]["content"])
            self.assertIn("借期 12 天", step["result"]["content"])
            unrelated = await ask(conversation, "帮我把‘谢谢’翻译成英文。")
            self.assertEqual(unrelated["result_status"], "completed", unrelated)
            ambiguous = await ask(conversation, "确认")
            self.assertIn("确认办理", ambiguous["messages"][-1]["content"])
            self.assertEqual((await ask(other_conversation, "确认办理"))["result_status"], "needs_input")
            self.assertFalse(self.gateway.writes)

            completed = await ask(conversation, natural_confirmation)
            self.assertEqual(completed["result_status"], "completed", completed)
            self.assertIn("已为你借阅", completed["steps"][0]["result"]["content"])
            path = f"/v1/runs/{completed['id']}"
            retry = await self.client.post(path + "/retry", json={"attempt": 1},
                                           headers={**student, "Idempotency-Key": str(uuid4())})
            self.assertEqual(retry.status_code, 202, retry.text)
            await worker.process(await self.store.claim())
            result = (await self.client.get(path, headers=student)).json()
            self.assertEqual(result["result_status"], "completed", result)
            self.assertEqual(decisions.count(natural_confirmation), 1)
            self.assertEqual(confirmation_reviews, [("library.start", False)])
            self.assertEqual((await ask(conversation, "确认办理"))["result_status"], "needs_input")
            self.assertEqual(len(reviews), 2)
            self.assertEqual([write[:2] for write in self.gateway.writes], [("library", OPERATIONS["library"].create)])
            self.assertEqual(self.gateway.writes[0][2]["duration_days"], 12)

            request = f"麻烦把《馆藏》还回去，并{followup}。"
            planned = {**planned, "action": "library.close", "arguments": {}}
            pending = await ask(conversation, request)
            self.assertEqual(pending["result_status"], "partial", pending)
            self.assertIn("归还期限是最迟还书日期", pending["messages"][-1]["content"])
            self.assertIn("「确认」", pending["messages"][-1]["content"])
            completed = await ask(conversation, "确认。")
            self.assertEqual(completed["result_status"], "completed", completed)
            self.assertEqual(len(reviews), 3)
            self.assertEqual(confirmation_reviews, [("library.start", False), ("library.close", True)])
            self.assertEqual(self.gateway.writes[-1][:2], ("library", OPERATIONS["library"].close))
