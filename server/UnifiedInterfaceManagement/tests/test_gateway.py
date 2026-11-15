import base64
import gzip
import json
import unittest
from dataclasses import replace
from io import BytesIO
from unittest.mock import call, patch

import httpx2
from starlette.requests import ClientDisconnect

from gateway import UpstreamResponse, create_app
from local import serve_local
from request_auth import RequestAuthorizationUnavailable
from settings import Settings


ADMIN = "a" * 43
MCP = "m" * 43
MODEL = "l" * 43
USER = "test-user-session"
LOCAL_KEY = "local-model-key"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class Workers:
    def __init__(self):
        self.calls = []

    async def call(self, service, action, arguments):
        self.calls.append((service, action, arguments))
        return 200, {"action": action}


class TrackedStream(httpx2.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings(
            public_url="https://campus.example.com", main_url="http://main.test", rag_url="http://rag.test",
            llm_url="http://llm.test", mcp_url="http://mcp.test/mcp", minta_url="http://minta.test",
            admin_token=ADMIN, mcp_token=MCP,
            llm_token=MODEL, llm_upstream_key=LOCAL_KEY, rag_scope="public_campus",
        )
        self.seen = []
        self.workers = Workers()
        self.http = await self.client(self.settings)

    async def client(self, settings, *, origin="http://127.0.0.1:8090", peer="127.0.0.1"):
        app = create_app(settings, transport=httpx2.MockTransport(self.upstream), workers=self.workers)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=(peer, 30001)), base_url=origin,
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def upstream(self, request):
        self.seen.append(request)
        path = request.url.path
        if request.method == "DELETE" and path.startswith(("/v1/instances/", "/v1/conversations/")):
            return httpx2.Response(204)
        if path == "/v1/me":
            allowed = request.headers.get("authorization") == auth(USER)["Authorization"]
            return httpx2.Response(200 if allowed else 401, json={"valid": allowed})
        if path.startswith("/v1/auth/access"):
            authorization = request.headers.get("authorization")
            if authorization == auth("pending")["Authorization"]:
                return httpx2.Response(403, json={"error": {"code": "campus_profile_required", "message": "请登记校园资料"}})
            if path == "/v1/auth/access/schedule":
                if authorization == auth("teacher-session")["Authorization"]:
                    return httpx2.Response(204, headers={
                        "X-Campus-Schedule-Role": "teacher", "X-Campus-Staff-Number": "t002",
                    })
                if authorization == auth("unverified-teacher-session")["Authorization"]:
                    return httpx2.Response(204, headers={"X-Campus-Schedule-Role": "teacher"})
                if authorization == auth(USER)["Authorization"]:
                    return httpx2.Response(204, headers={"X-Campus-Schedule-Role": "student",
                                                        "X-Campus-Schedule-Number": "student-test-a"})
            allowed = authorization == auth(USER)["Authorization"]
            return httpx2.Response(204 if allowed else 401)
        if path.startswith("/api/campus-desk/wechat"):
            if path.endswith("/events"):
                return httpx2.Response(200, stream=TrackedStream(b"data: 1\n\n", b"data: 2\n\n"), headers={
                    "content-type": "text/event-stream; charset=utf-8", "set-cookie": "digest=private",
                })
            return httpx2.Response(200, json={"items": [], "page_size": 12, "has_more": False})
        if path.startswith("/internal/campus-desk/"):
            self.assertEqual(request.url.port, 8100)
            self.assertEqual(request.headers["x-campus-rumor-key"], "t" * 43)
            self.assertEqual(request.headers["x-campus-rumor-audience"], "student")
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("cookie", request.headers)
            if path.endswith("/tips"):
                self.assertEqual(json.loads(request.content), {"content": "补充一条校园传闻"})
                return httpx2.Response(201, json={"ok": True, "tid": 12})
            return httpx2.Response(200, json={"pending": [], "confirmed": []})
        if path in {"/v1/minta/activities", "/v1/minta/classrooms"}:
            if request.url.query:
                return httpx2.Response(422, json={"error": {"code": "invalid_query"}})
            items = [{"room_id": "room-a101", "name": "明理楼 A101", "capacity": 36, "reserved": True}]
            if path.endswith("activities"):
                items = [{"starts_at": "2026-11-13T19:30:00+08:00", "ends_at": "2026-11-13T21:00:00+08:00"}]
            return httpx2.Response(200, json={"source": "minta_simulation", "items": items})
        if path == "/v1/minta/semester-report":
            if request.headers.get("authorization") != auth(USER)["Authorization"]:
                return httpx2.Response(401, json={"error": {"code": "invalid_credentials"}})
            return httpx2.Response(200, json={"source": "minta_simulation", "total_points": 7})
        if request.url.host == "minta.test":
            call = json.loads(request.content)
            course = {
                "course_id": "intro", "course_name": "新闻概论", "teacher": "张老师", "classroom": "教学楼 101",
                "day": "周一", "start_period": 1, "end_period": 2, "weeks": [1, 2, 3], "points": 3,
            }
            periods = [
                {"period": 1, "start_time": "08:00", "end_time": "08:45"},
                {"period": 2, "start_time": "08:55", "end_time": "09:40"},
            ]
            if call["target"] == "/classroom/api/availability":
                reply = {"rooms": [{
                    "roomId": "room-a101", "name": "明理楼 A101", "capacity": 36, "reserved": True,
                    "userName": "private", "reservationId": "private",
                }]}
                return httpx2.Response(200, json={"status": 200, "body": json.dumps(reply)})
            if call["target"] == "/schedule/agent/call":
                data = {"student_id": "student-test-a", "courses": [course], "periods": periods}
            elif call["target"].startswith("/schedule/api/teaching/"):
                data = {"courses": [{**course, "course_name": "授课样例", "staff_number": "t002"}], "periods": periods}
                return httpx2.Response(200, json={"status": 200, "body": json.dumps(data)})
            else:
                data = {"activities": [{
                    "id": "film", "name": "电影放映", "description": "校园电影放映", "location": "报告厅",
                    "starts_at": "2026-11-13T19:30:00+08:00", "ends_at": "2026-11-13T21:00:00+08:00",
                    "capacity": 40, "remaining": 12, "internal": "private",
                }]}
            reply = {"content": json.dumps(data)}
            return httpx2.Response(200, json={"status": 200, "body": json.dumps(reply)})
        if path == "/v1/auth/sso/campus/start":
            return httpx2.Response(200, json={}, headers=[
                ("Set-Cookie", "browser=first; Path=/; HttpOnly"),
                ("Set-Cookie", "extra=second; Path=/; HttpOnly"),
                ("Connection", "private-header"),
                ("Private-Header", "upstream-only"),
            ])
        if path.startswith("/v1/campus-view/media/"):
            return httpx2.Response(206, stream=TrackedStream(b"\x00\x01\xff"), headers={
                "content-type": "video/mp4", "content-range": "bytes 0-2/20",
            })
        if path in {"/mcp", "/v1/chat/completions"}:
            return httpx2.Response(200, stream=TrackedStream(b"data: first\n\n", b"data: last\n\n"), headers={
                "content-type": "text/event-stream", "mcp-session-id": "session-id",
            })
        return httpx2.Response(200, json={"path": path})

    async def test_credentials_are_separate(self):
        for token in (USER, MODEL, MCP):
            self.assertEqual((await self.http.get("/admin/interfaces", headers=auth(token))).status_code, 401)
        self.assertEqual((await self.http.get("/admin/mcp", headers=auth(ADMIN))).status_code, 401)
        self.assertEqual((await self.http.get("/admin/llm/v1/models", headers=auth(USER))).status_code, 401)
        self.assertEqual((await self.http.get("/v1/me")).status_code, 401)
        self.assertEqual((await self.http.get("/v1/me", headers=auth(ADMIN))).status_code, 401)
        self.assertFalse(self.seen)
        self.assertEqual((await self.http.get("/v1/me", headers=auth(USER))).status_code, 200)
        self.assertEqual(self.seen[-1].url.host, "main.test")
        result = await self.http.get("/admin/postgres/ready", headers={**auth(ADMIN), "Cookie": "one=two"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual((self.seen[-1].url.host, self.seen[-1].url.path), ("main.test", "/ready"))
        self.assertNotIn("authorization", self.seen[-1].headers)
        self.assertNotIn("cookie", self.seen[-1].headers)

    async def test_campus_photo_upload_limit_only_applies_to_the_photo_route(self):
        path = "/v1/campus-view/photo-places/1165519_399134/photos"
        headers = {**auth(USER), "Content-Type": "application/octet-stream", "Idempotency-Key": "test-photo"}
        photo = b"x" * (20 * 1024 * 1024)
        response = await self.http.post(path, headers=headers, content=photo)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen[-1].headers["idempotency-key"], "test-photo")
        self.assertEqual(self.seen[-1].content, photo)

        for route, body in ((path, photo + b"x"), ("/v1/campus-view/resolve", photo)):
            response = await self.http.post(route, headers=headers, content=body)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (413, "request_too_large"))
        self.assertEqual(len(self.seen), 1)

    async def test_wechat_feed_and_events_require_campus_identity_and_keep_the_token_private(self):
        path = "/v1/campus-digest/wechat"
        self.assertEqual((await self.http.get(path)).status_code, 401)
        self.assertEqual((await self.http.get(path + "/events", headers=auth("pending"))).status_code, 403)
        self.assertFalse(any(request.url.port == 8184 for request in self.seen))

        response = await self.http.get(path, params={"account": "学生会", "title": "志愿活动", "offset": 12},
                                       headers={**auth(USER), "Cookie": "private=yes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen[-2].url.path, "/v1/auth/access")
        forwarded = self.seen[-1]
        self.assertEqual((forwarded.url.host, forwarded.url.port, forwarded.url.path),
                         ("127.0.0.1", 8184, "/api/campus-desk/wechat"))
        self.assertEqual(dict(forwarded.url.params), {"account": "学生会", "title": "志愿活动", "offset": "12"})
        self.assertNotIn("authorization", forwarded.headers)
        self.assertNotIn("cookie", forwarded.headers)

        long_filters = {"account": "学" * 120, "title": "文" * 200}
        self.assertEqual((await self.http.get(path, params=long_filters, headers=auth(USER))).status_code, 200)
        self.assertEqual(dict(self.seen[-1].url.params), long_filters)

        events = await self.http.get(path + "/events", headers=auth(USER))
        self.assertEqual((events.status_code, events.content), (200, b"data: 1\n\ndata: 2\n\n"))
        self.assertEqual(events.headers["x-accel-buffering"], "no")
        self.assertNotIn("set-cookie", events.headers)

    async def test_rumor_feed_and_feedback_require_completed_campus_identity(self):
        client = await self.client(replace(self.settings, campus_rumor_key="t" * 43))
        path = "/v1/campus-rumor"
        self.assertEqual((await client.get(path)).status_code, 401)
        self.assertEqual((await client.get(path, headers=auth("pending"))).status_code, 403)
        self.assertFalse(any(request.url.port == 8100 for request in self.seen))
        self.assertEqual((await client.get(path, headers={**auth(USER), "Cookie": "private=yes"})).json(),
                         {"pending": [], "confirmed": []})
        self.assertEqual((await client.post(path + "/tips", json={"content": "太短"}, headers=auth(USER))).status_code, 422)
        sent = await client.post(path + "/tips", json={"content": "补充一条校园传闻"}, headers=auth(USER))
        self.assertEqual((sent.status_code, sent.json()), (201, {"ok": True, "tid": 12}))

    async def test_postgres_sessions_and_rag_documents_use_distinct_routes(self):
        ordinary = (await self.http.get("/v1/interfaces")).json()["interfaces"]
        self.assertEqual(
            {entry["data_domain"] for entry in ordinary if entry["path"] in {
                "/v1/me", "/v1/auth/sessions", "/v1/conversations/{conversation_id}/messages",
            }}, {"postgresql"},
        )
        self.assertEqual(next(entry["data_domain"] for entry in ordinary if entry["path"] == "/v1/rag/search"), "milvus")
        response = await self.http.get("/v1/conversations/abc/messages?limit=20&cursor=next", headers=auth(USER))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen[-1].url.host, "main.test")
        self.assertEqual(self.seen[-1].url.query, b"limit=20&cursor=next")
        response = await self.http.post("/v1/rag/search", headers=auth(USER), json={"query": "library", "scope": "private"})
        self.assertEqual(response.status_code, 403)
        response = await self.http.post("/v1/rag/search", headers=auth("expired"), json={"query": "library"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual([request.url.host for request in self.seen], ["main.test", "main.test"])
        response = await self.http.post(
            "/v1/rag/search",
            headers={**auth(USER), "Cookie": "id=private", "X-User-ID": "u1", "Idempotency-Key": "user-private-key"},
            json={"query": "library"},
        )
        self.assertEqual(response.status_code, 200)
        verification, search = self.seen[-2:]
        self.assertEqual((verification.url.host, verification.url.path), ("main.test", "/v1/auth/access"))
        self.assertEqual((search.url.host, search.url.path), ("rag.test", "/search"))
        self.assertEqual(json.loads(search.content), {"query": "library", "scope": "public_campus", "top_k": 5})
        for header in ("authorization", "cookie", "x-user-id", "idempotency-key", "x-forwarded-for"):
            self.assertNotIn(header, search.headers)
        response = await self.http.post(
            "/admin/rag/search", headers=auth(ADMIN), json={"query": "library", "scope": "private"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.seen[-1].content)["scope"], "private")

    async def test_sso_cookie_stays_in_its_client(self):
        response = await self.http.post("/v1/auth/sso/campus/start", headers=auth(USER), json={"mode": "link"})
        self.assertEqual(self.seen[-1].headers["authorization"], auth(USER)["Authorization"])
        self.assertEqual(len(response.headers.get_list("set-cookie")), 2)
        self.assertNotIn("private-header", response.headers)
        response = await self.http.get("/v1/auth/sso/campus/callback?state=a%2Bb&ticket=t%2F1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("browser=first", self.seen[-1].headers["cookie"])
        self.assertEqual(self.seen[-1].url.query, b"state=a%2Bb&ticket=t%2F1")
        self.http.cookies.clear()
        await self.http.get("/v1/auth/sso/campus/callback")
        self.assertNotIn("cookie", self.seen[-1].headers)

    async def test_instance_and_conversation_management_forward_to_main_agent(self):
        conversation = "/v1/conversations/sample"
        instance = "/v1/instances/sample"
        renamed = await self.http.patch(conversation, headers=auth(USER), json={"title": "新标题"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(json.loads(self.seen[-1].content), {"title": "新标题"})
        for path in (conversation, instance):
            self.assertEqual((await self.http.delete(path)).status_code, 401)
            deleted = await self.http.delete(path, headers=auth(USER))
            self.assertEqual((deleted.status_code, deleted.content), (204, b""))
            request = self.seen[-1]
            self.assertEqual((request.method, request.url.host, request.url.path), ("DELETE", "main.test", path))
            self.assertEqual(request.headers["authorization"], auth(USER)["Authorization"])

    async def test_minta_shortcuts_authenticate_and_only_expose_selected_reads(self):
        schedule = "/v1/minta/schedule"
        activities = "/v1/minta/activities"
        classrooms = "/v1/minta/classrooms"
        report = "/v1/minta/semester-report"
        catalog = (await self.http.get("/v1/interfaces")).json()["interfaces"]
        self.assertEqual(
            {(item["path"], tuple(item["methods"]), item["access"], item["data_domain"])
             for item in catalog if item["path"].startswith("/v1/minta/")},
            {(path, ("GET",), "user", "minta_simulation") for path in (schedule, activities, classrooms, report)} | {
                ("/v1/minta/operations", ("GET", "POST"), "user", "postgresql"),
                ("/v1/minta/operations/{operation_id}", ("DELETE",), "user", "postgresql"),
                ("/v1/minta/catalog/{service}", ("GET",), "user", "minta_simulation"),
                ("/v1/minta/timetable", ("GET",), "user", "minta_simulation"),
                ("/v1/minta/teacher-credential", ("GET",), "user", "teacher_credentials"),
                ("/v1/minta/clubs", ("GET", "POST"), "user", "minta_simulation"),
            },
        )
        for path in (schedule, activities, classrooms, report):
            self.assertEqual((await self.http.get(path)).status_code, 401)
            self.assertEqual((await self.http.get(path, headers=auth(ADMIN))).status_code, 401)
        self.assertEqual((await self.http.get(schedule, headers=auth("expired"))).status_code, 401)
        self.assertEqual([request.url.host for request in self.seen], ["main.test"])
        pending = await self.http.get(schedule, headers=auth("pending"))
        self.assertEqual((pending.status_code, pending.json()["error"]["code"]), (403, "campus_profile_required"))

        for path in (
            schedule + "?student_id=2025002", schedule + "?week=0", schedule + "?week=21",
            schedule + "?week=2&week=3", activities + "?target=/club/agent/call",
        ):
            self.assertEqual((await self.http.get(path, headers=auth(USER))).status_code, 422)
        self.assertEqual((await self.http.post(schedule, headers=auth(USER), json={})).status_code, 405)
        self.assertEqual((await self.http.post("/v1/minta/call", headers=auth(USER), json={})).status_code, 404)
        self.assertTrue(all(request.url.host == "main.test" for request in self.seen))

        response = await self.http.get(schedule + "?week=3", headers={**auth(USER), "Cookie": "session=private"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["source"], body["week"], body["items"][0]["course_name"]), (
            "minta_simulation", 3, "新闻概论",
        ))
        self.assertEqual(body["audience"], "student")
        self.assertEqual(body["periods"][1]["start_time"], "08:55")
        self.assertEqual(body["items"][0]["points"], 3)
        self.assertEqual((body["items"][0]["start_time"], body["items"][0]["end_time"]), ("08:00", "09:40"))
        self.assertNotIn("student_id", body)
        self.assertNotIn("weeks", body["items"][0])
        verification, upstream = self.seen[-2:]
        self.assertEqual(verification.url.path, "/v1/auth/access/schedule")
        self.assertEqual((upstream.method, upstream.url.host, upstream.url.path), ("POST", "minta.test", "/v1/call"))
        self.assertEqual(json.loads(upstream.content), {
            "method": "POST", "target": "/schedule/agent/call", "timeout_ms": 5000,
            "json": {"name": "schedule_query", "arguments": {"student_id": "student-test-a", "week": 3}},
        })
        self.assertNotIn("authorization", upstream.headers)
        self.assertNotIn("cookie", upstream.headers)

        response = await self.http.get(activities, headers=auth(USER))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["starts_at"], "2026-11-13T19:30:00+08:00")
        self.assertEqual(response.json()["items"][0]["ends_at"], "2026-11-13T21:00:00+08:00")
        self.assertNotIn("internal", response.json()["items"][0])
        self.assertEqual((self.seen[-1].url.host, self.seen[-1].url.path), ("main.test", activities))

        response = await self.http.get(classrooms, headers=auth(USER))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"source": "minta_simulation", "items": [{
            "room_id": "room-a101", "name": "明理楼 A101", "capacity": 36, "reserved": True,
        }]})
        self.assertEqual((self.seen[-1].url.host, self.seen[-1].url.path), ("main.test", classrooms))
        self.assertEqual(self.seen[-1].headers["authorization"], auth(USER)["Authorization"])
        response = await self.http.get(report, headers=auth(USER))
        self.assertEqual((response.status_code, response.json()["total_points"]), (200, 7))
        self.assertEqual((self.seen[-1].url.host, self.seen[-1].url.path), ("main.test", report))
        self.assertEqual(self.seen[-1].headers["authorization"], auth(USER)["Authorization"])

    async def test_teacher_schedule_uses_only_the_staff_number_from_mainagent(self):
        path = "/v1/minta/schedule"
        headers = {**auth("teacher-session"), "X-Campus-Staff-Number": "t999", "X-Campus-Schedule-Role": "student"}
        self.assertEqual((await self.http.get(path + "?staff_number=t999", headers=headers)).status_code, 422)
        response = await self.http.get(path + "?week=2", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["audience"], "teacher")
        self.assertTrue(response.json()["staff_verified"])
        self.assertEqual(response.json()["items"][0]["course_name"], "授课样例")
        self.assertNotIn("staff_number", response.json()["items"][0])
        self.assertNotIn("X-Campus-Staff-Number", response.headers)
        verification, upstream = self.seen[-2:]
        self.assertNotIn("X-Campus-Staff-Number", verification.headers)
        self.assertNotIn("X-Campus-Schedule-Role", verification.headers)
        self.assertEqual(json.loads(upstream.content), {
            "method": "GET", "target": "/schedule/api/teaching/t002?week=2", "timeout_ms": 5000,
        })
        self.assertNotIn("authorization", upstream.headers)

        previous = len(self.seen)
        response = await self.http.get(path, headers=auth("unverified-teacher-session"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["staff_verified"])
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(len(self.seen), previous + 1)

    async def test_mcp_model_and_media_keep_transport_semantics(self):
        response = await self.http.post("/admin/mcp", headers={
            **auth(MCP), "MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": "session-in",
            "Accept": "application/json, text/event-stream",
        }, json={"method": "tools/list"})
        self.assertEqual(response.content, b"data: first\n\ndata: last\n\n")
        self.assertEqual(response.headers["mcp-session-id"], "session-id")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertEqual(self.seen[-1].headers["mcp-session-id"], "session-in")
        self.assertEqual(self.seen[-1].headers["mcp-protocol-version"], "2025-11-25")
        self.assertEqual(self.seen[-1].headers["authorization"], auth(MCP)["Authorization"])
        response = await self.http.post(
            "/admin/llm/v1/chat/completions", headers={**auth(MODEL), "OpenAI-Beta": "responses=v1"},
            json={"stream": True},
        )
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(self.seen[-1].headers["authorization"], auth(LOCAL_KEY)["Authorization"])
        self.assertEqual(self.seen[-1].headers["openai-beta"], "responses=v1")
        response = await self.http.get("/v1/campus-view/media/sample.mp4", headers={**auth(USER), "Range": "bytes=0-2"})
        self.assertEqual((response.status_code, response.content), (206, b"\x00\x01\xff"))
        self.assertEqual(response.headers["content-range"], "bytes 0-2/20")
        self.assertEqual(self.seen[-1].headers["range"], "bytes=0-2")

    async def test_remote_transport_boundary(self):
        public = await self.client(self.settings, origin="https://campus.example.com", peer="203.0.113.10")
        insecure = await self.client(self.settings, origin="http://campus.example.com", peer="203.0.113.10")
        self.assertEqual((await insecure.get("/health")).status_code, 426)
        denied = await public.get("/admin/interfaces", headers={**auth(ADMIN), "X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual((await public.get("/v1/me", headers={**auth(USER), "X-Forwarded-For": "127.0.0.1"})).status_code, 200)
        self.assertEqual(self.seen[-1].headers["x-forwarded-for"], "203.0.113.10")
        self.assertEqual(self.seen[-1].headers["x-forwarded-proto"], "https")
        enabled = await self.client(replace(self.settings, allow_remote_admin=True),
                                    origin="https://campus.example.com", peer="203.0.113.10")
        self.assertEqual((await enabled.get("/admin/interfaces", headers=auth(ADMIN))).status_code, 200)

    async def test_admin_worker_validates_request_before_accessing_disk(self):
        url = "/admin/rag/documents"
        data = {"filename": "../confidential.txt", "data_base64": "bGlicmFyeQ=="}
        response = await self.http.post(url, headers=auth(ADMIN), json=data)
        self.assertEqual(response.status_code, 422)
        response = await self.http.post(url, headers=auth(USER), json={"text": "library", "source": "notice"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.workers.calls)
        text = "校园图书馆开放通知\n" * 10_000
        response = await self.http.post(url, headers=auth(ADMIN), json={"text": text, "source": "notice"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.workers.calls[-1],
                         ("rag_worker", "ingest", {"text": text, "source": "notice", "scope": "public_campus"}))

    async def test_local_frames_reuse_the_same_authorization_and_body(self):
        items = [
            {"id": "denied", "method": "GET", "path": "/admin/interfaces", "headers": auth(USER)},
            {"id": "me", "method": "GET", "path": "/v1/me", "headers": auth(USER)},
            {"id": "bad", "method": "GET", "path": "//rag.test/scopes"},
            {"id": "media", "method": "GET", "path": "/v1/campus-view/media/sample.mp4", "headers": auth(USER)},
            {"id": "schedule", "method": "GET", "path": "/v1/minta/schedule?week=3", "headers": auth(USER)},
        ]
        input_lines = b"\n".join(json.dumps(item).encode() for item in items)
        output = BytesIO()
        app = create_app(self.settings, transport=httpx2.MockTransport(self.upstream), workers=self.workers)
        await serve_local(app, BytesIO(input_lines), output, scheme="https")
        frames = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([(frame["id"], frame["status"]) for frame in frames if "status" in frame],
                         [("denied", 401), ("me", 200), ("bad", 400), ("media", 206), ("schedule", 200)])
        media = [frame for frame in frames if frame["id"] == "media"]
        self.assertEqual([frame["type"] for frame in media], ["start", "data", "end"])
        self.assertEqual(base64.b64decode(media[1]["body_base64"]), b"\x00\x01\xff")
        schedule = next(frame for frame in frames if frame["id"] == "schedule" and frame["type"] == "data")
        self.assertEqual(json.loads(base64.b64decode(schedule["body_base64"]))["week"], 3)

    async def test_stream_closes_on_disconnect_and_keeps_compressed_bytes(self):
        compressed = gzip.compress(b"test bytes")
        stream = TrackedStream(compressed)
        request = httpx2.Request("GET", "http://main.test/data")
        upstream = httpx2.Response(
            200, stream=stream, request=request,
            headers={"content-encoding": "gzip", "content-length": str(len(compressed))},
        )
        response = UpstreamResponse(upstream, "main")

        async def send(message):
            if message["type"] == "http.response.body":
                self.assertEqual(message["body"], compressed)
                raise OSError("client disconnected")

        with self.assertRaises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
        self.assertTrue(stream.closed)

    async def test_managed_authorization_is_per_request_for_http_and_stdio(self):
        channel = r"\\.\pipe\mpm-auth-" + "a" * 32, 123
        settings = replace(self.settings, mpm_auth=channel)
        http = await self.client(settings)
        headers = auth(USER)
        with patch("gateway.authorize_request", side_effect=[
            False, True, False, True, RequestAuthorizationUnavailable(),
        ]) as gate:
            response = await http.get("/v1/instances", headers=headers)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "supervisor_denied"))
            self.assertFalse(self.seen)
            self.assertEqual((await http.get("/v1/instances")).status_code, 401)
            self.assertEqual((await http.get("/v1/instances", headers=headers)).status_code, 200)

            app = create_app(settings, transport=httpx2.MockTransport(self.upstream))
            local_request = {"id": "list", "method": "GET", "path": "/v1/instances", "headers": headers}
            incoming = BytesIO(b"\n".join(json.dumps({**local_request, "id": str(n)}).encode() for n in (1, 2)))
            output = BytesIO()
            await serve_local(app, incoming, output)
            frames = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([frame["status"] for frame in frames if frame["type"] == "start"], [403, 200])

            response = await http.get("/v1/instances", headers=headers)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "supervisor_unavailable"))
            self.assertEqual(gate.call_args_list, [call(*channel, "GET", "/v1/instances")] * 5)
            self.assertEqual(len(self.seen), 2)


if __name__ == "__main__":
    unittest.main()
