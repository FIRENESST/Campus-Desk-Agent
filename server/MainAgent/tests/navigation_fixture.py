"""Synthetic campus geometry for isolated tests."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@contextmanager
def navigation_fixture():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        campus_id = "fixture-campus"
        places = [
            {"id": "north-gate", "name": "北门", "node": "north"},
            {"id": "west-gate", "name": "西门", "node": "west"},
            {"id": "south-gate", "name": "南门", "node": "south"},
            {"id": "library", "name": "图书馆", "aliases": ["阅览室"], "node": "library"},
        ]
        nodes = {"north": [110.5005, 35.501], "west": [110.5, 35.5], "south": [110.5005, 35.499],
                 "junction": [110.5005, 35.5], "library": [110.5010, 35.5001]}
        source = "https://campus.example.test/maps/fixture"
        connections = [
            {"from_node": node, "to_node": "junction", "path": [nodes[node], nodes["junction"]],
             "name": "测试主路", "source": source}
            for node in ("north", "west", "south")
        ]
        connections.append({"from_node": "junction", "to_node": "library",
                            "path": [nodes["junction"], nodes["library"]],
                            "name": "测试图书馆道路", "source": source})

        def feature(kind, geometry, **properties):
            return {"type": "Feature", "properties": {"kind": kind, **properties}, "geometry": geometry}

        def point(coordinates):
            return {"type": "Point", "coordinates": coordinates}

        ring = [[110.499, 35.498], [110.502, 35.498], [110.502, 35.502],
                [110.499, 35.502], [110.499, 35.498]]
        metadata = {"campus_id": campus_id, "campus_name": "测试校园"}
        for name, data in {
            "base_map.geojson": {
                "type": "FeatureCollection", "metadata": metadata,
                "features": [
                    feature("campus_boundary", {"type": "Polygon", "coordinates": [ring]}),
                    feature("landmark", point(nodes["library"]), name="图书馆"),
                    feature("landmark", point([110.5012, 35.5003]), name="阅览室"),
                ],
            },
            "street_view.json": {
                **metadata, "points": [{"id": "library", "name": "图书馆", "coordinates": nodes["library"]}],
            },
            "walking_routes.json": {"campus_id": campus_id, "road_names": {},
                                    "places": places, "connections": connections},
        }.items():
            (root / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with patch.dict(os.environ, {"CAMPUS_NAVIGATION_DIR": str(root)}):
            yield root
