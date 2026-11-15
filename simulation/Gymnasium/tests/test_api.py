import unittest

from fastapi.testclient import TestClient

from app import create_app
from store import GymStore
from fixtures import gym_store


class RestApiTests(unittest.TestCase):
    def setUp(self):
        self.store = gym_store()
        self.addCleanup(self.store.close)
        self.client_context = TestClient(
            create_app(self.store), base_url="http://127.0.0.1"
        )
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def test_initial_venues_and_empty_reservations(self):
        venues_response = self.client.get("/api/venues")
        reservations_response = self.client.get("/api/reservations")

        self.assertEqual(200, venues_response.status_code)
        venues = venues_response.json()["venues"]
        self.assertEqual(7, len(venues))
        self.assertTrue({"student-center", "indoor-basketball", "indoor-badminton"} <= {venue["id"] for venue in venues})
        window = venues_response.json()["booking_window"]
        self.assertLess(window["first_date"], window["last_date"])
        self.assertEqual(2, venues[0]["capacity"])
        self.assertEqual(2, venues[0]["remaining"])
        self.assertEqual(200, reservations_response.status_code)
        self.assertEqual({"reservations": []}, reservations_response.json())

    def test_slot_directory_is_anonymous_and_reflects_reservations_and_cancellation(self):
        date = self.client.get("/api/venues").json()["booking_window"]["last_date"]
        params = {"venue_id": "indoor-badminton", "date": date}
        choices = self.client.get("/api/slots", params=params).json()["slots"]
        self.assertGreater(len(choices), 40)
        first = choices[0]
        booked = self.client.post("/api/reservations", json={
            "venue_id": params["venue_id"], "slot_id": first["slot_id"], "user_name": "测试账号",
        })
        self.assertEqual(booked.status_code, 201, booked.text)
        slots = self.client.get("/api/slots", params=params).json()["slots"]
        self.assertEqual(slots[0]["remaining"], 0)
        self.assertEqual(slots[1]["remaining"], 1)
        self.assertNotIn("user_name", slots[0])
        self.client.delete(f"/api/reservations/{booked.json()['reservation_id']}")
        self.assertEqual(self.client.get("/api/slots", params=params).json()["slots"][0]["remaining"], 1)

    def test_reserve_list_and_cancel_flow_uses_injected_store(self):
        response = self.client.post(
            "/api/reservations", json={"venue_id": "strength", "user_name": "张敏"}
        )

        self.assertEqual(201, response.status_code)
        reservation = response.json()
        self.assertEqual("张敏", reservation["user_name"])
        self.assertEqual(1, len(self.store.list_reservations()))
        listed = self.client.get("/api/reservations").json()["reservations"]
        self.assertEqual([reservation], listed)
        remaining = self.client.get("/api/venues").json()["venues"][0]["remaining"]
        self.assertEqual(1, remaining)

        cancelled = self.client.delete(
            f"/api/reservations/{reservation['reservation_id']}"
        )

        self.assertEqual(200, cancelled.status_code)
        self.assertTrue(cancelled.json()["cancelled"])
        self.assertEqual(reservation, cancelled.json()["reservation"])
        self.assertEqual([], self.store.list_reservations())
        remaining = self.client.get("/api/venues").json()["venues"][0]["remaining"]
        self.assertEqual(2, remaining)

    def test_business_errors_have_stable_json_and_http_status(self):
        first = {"venue_id": "strength", "user_name": "用户甲"}
        self.client.post("/api/reservations", json=first)

        duplicate = self.client.post("/api/reservations", json=first)
        self.assertEqual(409, duplicate.status_code)
        self.assertEqual(
            {"code": "CONFLICT", "retryable": False},
            {key: duplicate.json()[key] for key in ("code", "retryable")},
        )
        self.assertTrue(duplicate.json()["message"])

        self.client.post(
            "/api/reservations", json={"venue_id": "strength", "user_name": "用户乙"}
        )
        full = self.client.post(
            "/api/reservations", json={"venue_id": "strength", "user_name": "用户丙"}
        )
        self.assertEqual(409, full.status_code)
        self.assertEqual("CAPACITY_FULL", full.json()["code"])
        self.assertTrue(full.json()["retryable"])

        unknown = self.client.post(
            "/api/reservations", json={"venue_id": "missing", "user_name": "张敏"}
        )
        self.assertEqual(404, unknown.status_code)
        self.assertEqual("NOT_FOUND", unknown.json()["code"])

        unknown_cancel = self.client.delete("/api/reservations/missing")
        self.assertEqual(404, unknown_cancel.status_code)
        self.assertEqual("NOT_FOUND", unknown_cancel.json()["code"])

    def test_invalid_input_and_malformed_requests_return_invalid_argument(self):
        requests = [
            self.client.post(
                "/api/reservations", json={"venue_id": "", "user_name": "张敏"}
            ),
            self.client.post(
                "/api/reservations", json={"venue_id": "strength", "user_name": " "}
            ),
            self.client.post("/api/reservations", json={"venue_id": "strength"}),
            self.client.post(
                "/api/reservations", content="not-json", headers={"content-type": "application/json"}
            ),
        ]

        for response in requests:
            with self.subTest(status=response.status_code, body=response.text):
                self.assertEqual(400, response.status_code)
                self.assertEqual("INVALID_ARGUMENT", response.json()["code"])
                self.assertTrue(response.json()["message"])
                self.assertFalse(response.json()["retryable"])
        self.assertEqual([], self.store.list_reservations())


if __name__ == "__main__":
    unittest.main()
