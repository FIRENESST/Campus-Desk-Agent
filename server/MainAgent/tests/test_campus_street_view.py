import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from navigation_fixture import navigation_fixture

from main_agent import UnavailableToolError
from main_agent.campus_cluster import AgentId, AgentTask, build_campus_cluster
from main_agent.campus_cluster.subskills.campus_street_view import (
    StreetViewInput, StreetViewUnavailable, load_street_view, resolve_street_view, street_view_media,
)


class StreetViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.root = Path(temporary)
        self.enterContext(patch.dict(os.environ, {"CAMPUS_STREET_VIEW_DIR": str(self.root)}))
        self.manifest = {
            "campus_id": "test-campus", "campus_name": "测试校园",
            "points": [
                {"id": "south", "name": "南门", "aliases": ["入口"], "coordinates": [116.55, 39.91]},
                {"id": "north", "name": "北门", "aliases": ["入口"], "coordinates": [116.55, 39.913],
                 "content": [{"kind": "image", "file": "north.jpg"},
                             {"kind": "custom", "component": "notice", "props": {"priority": 1}}]},
            ],
            "routes": [{"id": "walk", "name": "采集路线", "point_ids": ["north", "south"]}],
        }
        self.save_manifest()

    def save_manifest(self):
        (self.root / "street_view.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    async def test_only_introduction_can_resolve_a_waypoint(self):
        cluster = build_campus_cluster()
        for role in AgentId:
            agent = cluster.registry.get(role)
            task = AgentTask("chat", "scene", agent.profile, "查街景")
            client = agent.skill_registry.bind(task)
            if role == AgentId.CAMPUS_INTRODUCTION:
                result = await client.call_tool("campus_street_view", {"point": "north"})
                self.assertEqual(json.loads(result.content)["point"]["detail_path"],
                                 "/v1/campus-view/points/north")
            else:
                with self.assertRaises(UnavailableToolError):
                    await client.call_tool("campus_street_view", {"point": "north"})

    def test_route_order_and_nearest_lookup_are_consistent(self):
        _, catalog = load_street_view()
        route = catalog.route("walk")
        self.assertEqual([point.id for point in catalog.ordered_points(route)], ["north", "south"])
        ambiguous = resolve_street_view(catalog, StreetViewInput(point="入口"))
        self.assertEqual(ambiguous["status"], "ambiguous")
        near = resolve_street_view(catalog, StreetViewInput(longitude=116.55001, latitude=39.913, route_id="walk"))
        self.assertEqual(near["point"]["id"], "north")
        far = resolve_street_view(catalog, StreetViewInput(longitude=116.56, latitude=39.913))
        self.assertEqual(far["status"], "no_match")

    def test_only_referenced_media_can_be_served(self):
        media = self.root / "media"
        media.mkdir()
        (media / "north.jpg").write_bytes(b"fixture")
        (media / "unlisted.jpg").write_bytes(b"private")
        root, catalog = load_street_view()
        self.assertEqual(street_view_media(root, catalog, "north.jpg"), (media / "north.jpg", "image/jpeg"))
        self.assertIsNone(street_view_media(root, catalog, "unlisted.jpg"))
        self.assertIsNone(street_view_media(root, catalog, "../street_view.json"))
        self.assertEqual(catalog.points[1].detail()["content"][1]["props"], {"priority": 1})

        self.manifest["routes"][0]["point_ids"].append("missing")
        self.save_manifest()
        with self.assertRaises(StreetViewUnavailable):
            load_street_view()


class CampusWalkingTests(unittest.TestCase):
    def test_synthetic_routes_connect_the_three_gates_to_the_library(self):
        self.enterContext(navigation_fixture())
        root, catalog = load_street_view()
        ambiguous = resolve_street_view(catalog, StreetViewInput(start="校门", destination="阅览室"), root)
        self.assertEqual(ambiguous["status"], "ambiguous")
        self.assertCountEqual(ambiguous["candidates"], ["北门", "西门", "南门"])
        for gate in ambiguous["candidates"]:
            with self.subTest(gate=gate):
                forward = resolve_street_view(catalog, StreetViewInput(start=gate, destination="阅览室"), root)
                back = resolve_street_view(catalog, StreetViewInput(start="图书馆", destination=gate), root)
                self.assertEqual((forward["status"], back["status"]), ("walking_route", "walking_route"))
                self.assertGreater(forward["distance_m"], 0)
                self.assertEqual(forward["distance_m"], back["distance_m"])
                self.assertEqual(forward["path"][0], forward["start"]["coordinates"])
                self.assertEqual(forward["path"][-1], forward["destination"]["coordinates"])
                self.assertEqual(forward["path"], back["path"][::-1])
                self.assertIn("测试图书馆道路", [step["road"] for step in forward["steps"]])
