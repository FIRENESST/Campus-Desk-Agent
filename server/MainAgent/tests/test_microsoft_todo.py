import asyncio
import json
import os
import unittest
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from uuid import uuid4

import test_cloud

if test_cloud.ENABLED:
    import httpx2
    from cryptography.fernet import Fernet

    from main_agent.cloud.microsoft_graph import marker
    from main_agent.cloud.microsoft_todo import MicrosoftTodo
    from main_agent.cloud.oidc import challenge


class TodoFixture:
    def __init__(self):
        self.token_grants = []
        self.lists = {}
        self.tasks = {"ordinary": {"id": "ordinary", "title": "自己的任务"}}
        self.writes = []
        self.lose_reply = False
        self.refuse = False
        self.calendar = {
            "courses": [{
                "course_id": "C123", "course_name": "媒体艺术", "classroom": "A101", "day": "周一",
                "start_period": 1, "end_period": 1, "weeks": [1],
            }],
            "periods": [{"period": 1, "start_time": "08:00", "end_time": "08:45"}],
        }

    async def request(self, request):
        path = request.url.path
        if request.url.host == "login.microsoftonline.com":
            fields = parse_qs(request.content.decode())
            self.token_grants.append(fields)
            return httpx2.Response(200, json={"access_token": "microsoft-access", "refresh_token": "microsoft-refresh",
                                             "expires_in": 3600})
        if request.headers.get("authorization") != "Bearer microsoft-access":
            return httpx2.Response(401)
        if path == "/v1.0/me":
            return httpx2.Response(200, json={"id": "microsoft-person", "displayName": "同步账号"})
        if self.refuse:
            return httpx2.Response(429, headers={"retry-after": "90"})
        method = request.method
        if path == "/v1.0/me/todo/lists":
            if method == "GET":
                return httpx2.Response(200, json={"value": list(self.lists.values())})
            item = {**json.loads(request.content), "id": "list-1", "isOwner": True, "isShared": False}
            self.lists[item["id"]] = item
            return httpx2.Response(201, json=item)
        if path == "/v1.0/me/todo/lists/list-1":
            return httpx2.Response(200, json=self.lists["list-1"])
        if path == "/v1.0/me/todo/lists/list-1/tasks" and method == "GET":
            return httpx2.Response(200, json={"value": list(self.tasks.values())})
        self.writes.append(method)
        if method == "POST":
            identifier = str(uuid4())
            task = {**json.loads(request.content), "id": identifier, "status": "notStarted"}
            self.tasks[identifier] = task
            if self.lose_reply:
                self.lose_reply = False
                raise httpx2.ReadTimeout("simulated lost create response")
            return httpx2.Response(201, json=task)
        identifier = path.rsplit("/", 1)[-1]
        if method == "PATCH":
            self.tasks[identifier].update(json.loads(request.content))
            return httpx2.Response(200, json=self.tasks[identifier])
        del self.tasks[identifier]
        return httpx2.Response(204)

    async def tool(self, service, name, args):
        if service == "schedule":
            return self.calendar
        raise AssertionError((service, name, args))


class MicrosoftTodoTests(test_cloud.CloudPostgresTestCase):
    def configure_settings(self, settings):
        return replace(settings, todo_client_id=str(uuid4()), todo_client_secret="fixture",
                       todo_redirect_uri="https://campus.test/oauth/microsoft-todo/callback",
                       todo_token_key=Fernet.generate_key().decode())

    def extra_app_arguments(self):
        self.fixture = TodoFixture()
        return {"minta_gateway": self.fixture, "todo_transport": httpx2.MockTransport(self.fixture.request)}

    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {"CLOUD_TERM_ID": "fixture-term",
                          "CLOUD_TERM_NAME": "测试学期", "CLOUD_TERM_START": "2026-09-07"}))

        async def no_background_sync(self):
            await asyncio.Event().wait()
        with patch.object(MicrosoftTodo, "run", no_background_sync):
            await super().asyncSetUp()
        self.enterContext(patch.object(self.app.state.campus_access, "schedule_number",
                                       return_value="student-test-a"))

    async def test_oauth_is_single_use_and_tasks_follow_the_account_schedule(self):
        path = "/v1/calendar/microsoft-todo"
        alice, _ = await self.account("alice")
        bob, _ = await self.account("bob")
        self.assertEqual((await self.client.get(path)).status_code, 401)
        status = (await self.client.get(path, headers=alice)).json()
        self.assertTrue(status["configured"])
        self.assertIsNone(status["connection"])
        start = await self.client.post(path + "/start", headers=alice)
        self.assertEqual(start.status_code, 200, start.text)
        url = urlsplit(start.json()["authorization_url"])
        query = parse_qs(url.query)
        self.assertEqual(url.hostname, "login.microsoftonline.com")
        self.assertEqual(query["redirect_uri"], [self.config.todo_redirect_uri])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("Tasks.ReadWrite", query["scope"][0])
        self.assertIn("offline_access", query["scope"][0])
        finish = {"state": query["state"][0], "code": "test-code"}
        self.assertEqual((await self.client.post(path + "/finish", headers=bob, json=finish)).status_code, 409)
        self.assertEqual(self.fixture.token_grants, [])
        self.assertEqual((await self.client.post(path + "/finish", headers=alice, json=finish)).status_code, 204)
        self.assertEqual(challenge(self.fixture.token_grants[0]["code_verifier"][0]), query["code_challenge"][0])
        self.assertEqual((await self.client.post(path + "/finish", headers=alice, json=finish)).status_code, 409)
        self.assertIsNone((await self.client.get(path, headers=bob)).json()["connection"])
        self.assertNotIn("token", (await self.client.get(path, headers=alice)).text)

        row = await self.db.run(lambda c: c.execute("SELECT * FROM microsoft_todo_connections").fetchone())
        self.assertNotIn(b"microsoft-refresh", bytes(row["refresh_token"]))
        self.assertNotIn(b"microsoft-access", bytes(row["access_token"]))
        todo = self.app.state.microsoft_todo

        async def queue():
            response = await self.client.post(path + "/sync", headers=alice)
            self.assertEqual(response.status_code, 202)

        # The first Graph create commits remotely, then its response is lost.
        self.fixture.lose_reply = True
        self.assertTrue(await todo.run_one())
        stored = (await self.client.get(path, headers=alice)).json()["connection"]
        self.assertEqual(stored["sync_error"], "microsoft_unavailable")
        identifier, task = next((key, value) for key, value in self.fixture.tasks.items() if key != "ordinary")
        self.assertEqual(task["dueDateTime"], {"dateTime": "2026-09-07T08:45:00", "timeZone": "China Standard Time"})
        self.assertRegex(marker(task), r"^v1:task:[a-f0-9]{64}:[a-f0-9]{64}$")
        task["status"] = "completed"
        await queue()
        self.assertTrue(await todo.run_one())
        self.assertEqual(self.fixture.writes, ["POST"])
        self.assertEqual(len(self.fixture.lists), 1)

        self.fixture.calendar["periods"][0]["end_time"] = "09:00"
        await queue()
        self.assertTrue(await todo.run_one())
        self.assertEqual(self.fixture.writes, ["POST", "PATCH"])
        self.assertEqual(self.fixture.tasks[identifier]["status"], "completed")
        self.assertIn("09:00", self.fixture.tasks[identifier]["body"]["content"])
        self.fixture.calendar["courses"] = []
        await queue()
        self.assertTrue(await todo.run_one())
        self.assertEqual(self.fixture.writes, ["POST", "PATCH", "DELETE"])
        self.assertIn("ordinary", self.fixture.tasks)
        self.assertNotIn(identifier, self.fixture.tasks)

        self.fixture.refuse = True
        await queue()
        self.assertTrue(await todo.run_one())
        self.assertEqual((await self.client.get(path, headers=alice)).json()["connection"]["sync_error"], "microsoft_throttled")
        await queue()
        self.assertFalse(await todo.run_one())
        self.assertEqual((await self.client.delete(path, headers=bob)).status_code, 204)
        self.assertIsNotNone((await self.client.get(path, headers=alice)).json()["connection"])
        self.assertEqual((await self.client.delete(path, headers=alice)).status_code, 204)
        self.assertIsNone((await self.client.get(path, headers=alice)).json()["connection"])


if __name__ == "__main__":
    unittest.main()
