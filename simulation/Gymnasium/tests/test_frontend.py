import unittest

from fastapi.testclient import TestClient

from app import create_app
from store import GymStore
from fixtures import gym_store


class FrontendTests(unittest.TestCase):
    def setUp(self):
        self.client_context = TestClient(
            create_app(gym_store()), base_url="http://127.0.0.1:8101"
        )
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)

    def test_dashboard_is_self_contained_responsive_chinese_html(self):
        response = self.client.get("/")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/html", response.headers["content-type"])
        html = response.text
        self.assertIn('<html lang="zh-CN">', html)
        self.assertIn('name="viewport"', html)
        self.assertIn('id="venue-grid"', html)
        self.assertIn('id="reservation-form"', html)
        self.assertIn('id="reservation-list"', html)
        self.assertIn("场地预约", html)
        self.assertIn("取消预约", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("https://", html)

    def test_local_assets_call_same_origin_rest_and_support_mobile_layout(self):
        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")

        self.assertEqual(200, script.status_code)
        self.assertIn('"/api/venues"', script.text)
        self.assertIn('"/api/reservations"', script.text)
        self.assertIn("cancelReservation", script.text)
        self.assertIn("addEventListener", script.text)
        self.assertNotIn("https://", script.text)
        self.assertEqual(200, styles.status_code)
        self.assertIn("@media", styles.text)
        self.assertIn("prefers-reduced-motion", styles.text)


if __name__ == "__main__":
    unittest.main()
