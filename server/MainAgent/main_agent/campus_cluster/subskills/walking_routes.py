"""Walk the campus road graph and return turns without joining unmapped roads."""

import heapq
import json
from collections import defaultdict
from dataclasses import dataclass
from math import asin, atan2, cos, degrees, radians, sin, sqrt


_DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
_GATES = {"门", "校门", "大门", "校区门口", "学校门口", "门口"}
_OSM = "https://www.openstreetmap.org/copyright"


@dataclass(frozen=True)
class Segment:
    name: str
    path: tuple
    distance: float
    source: str


def distance_m(start, end):
    lng1, lat1 = map(radians, start)
    lng2, lat2 = map(radians, end)
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lng2 - lng1) / 2) ** 2
    return 12_742_000 * asin(sqrt(min(1.0, a)))


def _path(points):
    path = tuple(tuple(point) for point in points)
    if len(path) < 2 or any(len(point) != 2 or not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90) for point in path):
        raise ValueError("无效的路线坐标")
    return path


def _load(root, campus_id):
    with (root / "base_map.geojson").open(encoding="utf-8") as stream:
        base = json.load(stream)
    with (root / "walking_routes.json").open(encoding="utf-8") as stream:
        config = json.load(stream)
    if config["campus_id"] != campus_id or base["metadata"]["campus_id"] != campus_id:
        raise ValueError("路网与当前校区不一致")
    graph, nodes = defaultdict(list), {}
    overrides, replaced = config["road_names"], set(config.get("replaced_edges", ()))

    def add(start, end, path, name, source):
        if nodes.setdefault(start, path[0]) != path[0] or nodes.setdefault(end, path[-1]) != path[-1]:
            raise ValueError("同一路网节点的位置不一致")
        length = sum(distance_m(a, b) for a, b in zip(path, path[1:]))
        if length > 0:
            graph[start].append((end, Segment(name, path, length, source)))
            graph[end].append((start, Segment(name, path[::-1], length, source)))

    for feature in base["features"]:
        data = feature["properties"]
        if data["kind"] != "walkway_candidate" or feature["id"] in replaced:
            continue
        tags = data.get("tags", {})
        if tags.get("foot") in {"no", "private"} or (
            tags.get("access") in {"no", "private"} and tags.get("foot") not in {"yes", "designated", "permissive"}
        ):
            continue
        add(data["from_node"], data["to_node"], _path(feature["geometry"]["coordinates"]),
            overrides.get(feature["id"]) or data.get("name") or "校内道路", _OSM)
    for item in config["connections"]:
        add(item["from_node"], item["to_node"], _path(item["path"]), item["name"], item["source"])
    places = config["places"]
    if any(place["node"] not in nodes for place in places):
        raise ValueError("有地点未接入步行路网")
    return graph, nodes, places


def _place_names(place):
    return (place["id"], place["name"], *place.get("aliases", ()))


def walking_places(root, campus_id):
    try:
        _, nodes, places = _load(root, campus_id)
        return [{"id": place["id"], "name": place["name"], "coordinates": nodes[place["node"]]} for place in places]
    except (OSError, ValueError, TypeError, KeyError):
        return []


def _matches(value, places):
    term = value.casefold().replace(" ", "")
    for prefix in ("中国传媒大学北京校区", "中传北京校区", "中国传媒大学", "中传"):
        if term.startswith(prefix) and term != prefix:
            term = term[len(prefix):].removeprefix("的")
            break
    if term in _GATES:
        return [place for place in places if place["id"].endswith("-gate")]
    exact = [place for place in places if term in {name.casefold() for name in _place_names(place)}]
    return exact or [place for place in places if any(term in name.casefold() for name in _place_names(place))]


def _shortest(graph, start, end):
    costs = {start: 0.0}
    parent = {}
    queue = [(0.0, start)]
    while queue:
        cost, node = heapq.heappop(queue)
        if node == end:
            segments = []
            while node != start:
                node, segment = parent[node]
                segments.append(segment)
            return segments[::-1]
        if cost != costs[node]:
            continue
        for other, segment in graph[node]:
            candidate = cost + segment.distance
            if candidate < costs.get(other, float("inf")):
                costs[other] = candidate
                parent[other] = node, segment
                heapq.heappush(queue, (candidate, other))
    return None


def _direction(start, end):
    dx = (end[0] - start[0]) * cos(radians((start[1] + end[1]) / 2))
    angle = degrees(atan2(dx, end[1] - start[1]))
    return _DIRECTIONS[round(angle / 45) % 8]


def _steps(segments):
    rows = []
    path = [segments[0].path[0]]
    for segment in segments:
        path.extend(segment.path[1:])
        for start, end in zip(segment.path, segment.path[1:]):
            distance = distance_m(start, end)
            if distance < 1:
                continue
            direction = _direction(start, end)
            if rows and rows[-1]["road"] == segment.name and (
                rows[-1]["direction"] == direction or distance < 12
            ):
                rows[-1]["distance_m"] += distance
            else:
                rows.append({"road": segment.name, "direction": direction, "distance_m": distance})
    for row in rows:
        row["distance_m"] = max(1, round(row["distance_m"]))
    return rows, path


def resolve_walk(root, campus_id, start, destination):
    try:
        graph, nodes, places = _load(root, campus_id)
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "walking_routes_unavailable"}
    endpoints = {}
    for field, value in (("start", start), ("destination", destination)):
        candidates = _matches(value, places)
        if len(candidates) != 1:
            return {"status": "ambiguous" if candidates else "no_match", "field": field,
                    "candidates": [place["name"] for place in candidates[:8]]}
        endpoints[field] = candidates[0]
    source, target = endpoints["start"]["node"], endpoints["destination"]["node"]
    points = {field: {"name": place["name"], "coordinates": nodes[place["node"]], "note": place.get("note")}
              for field, place in endpoints.items()}
    if source == target:
        return {"status": "same_place", **points}
    segments = _shortest(graph, source, target)
    if segments is None:
        return {"status": "no_connected_route", **points}
    steps, path = _steps(segments)
    return {"status": "walking_route", **points, "distance_m": round(sum(segment.distance for segment in segments)),
            "steps": steps, "path": path, "sources": list(dict.fromkeys([_OSM, *(part.source for part in segments)])),
            "access": "按校内现行通行标识和开放情况行走。"}
