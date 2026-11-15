from io import BytesIO

import test_cloud

if test_cloud.ENABLED:
    from PIL import Image, PngImagePlugin

    from main_agent.cloud.admin_data import _execute
    from main_agent.cloud.errors import ServiceError


def make_photo(color, padding=0):
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private source metadata" + "x" * padding)
    Image.new("RGB", (160, 110), color).save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


class CampusPhotoTests(test_cloud.CloudPostgresTestCase):
    async def asyncSetUp(self):
        from navigation_fixture import navigation_fixture
        from main_agent.cloud.campus_photos import _campus_ring, _map_features, _place_names
        self.enterContext(navigation_fixture())
        for cached in (_campus_ring, _map_features, _place_names):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)
        await super().asyncSetUp()

    async def test_original_photo_upload_allows_exactly_20_mb(self):
        headers, _ = await self.account()
        headers = {**headers, "Content-Type": "application/octet-stream", "Idempotency-Key": "upload-limit"}
        gallery = "/v1/campus-view/photo-places/1105010_355001/photos"
        limit = 20 * 1024 * 1024
        photo = make_photo("navy", limit - len(make_photo("navy")))
        self.assertEqual(len(photo), limit)
        oversized = await self.client.post(gallery, content=photo + b"x", headers=headers)
        self.assertEqual((oversized.status_code, oversized.json()["error"]["code"]), (413, "body_too_large"))
        accepted = await self.client.post(gallery, content=photo, headers=headers)
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()["status"], "pending")

    async def test_moderation_controls_public_places_galleries_and_images(self):
        uploader, _ = await self.account()
        viewer, _ = await self.account("bob")
        operator = (await self.client.get("/v1/me", headers=viewer)).json()["id"]
        places = "/v1/campus-view/photo-places"
        location = "1105010_355001"
        gallery = f"{places}/{location}/photos"
        raw = make_photo("navy")
        headers = {**uploader, "Content-Type": "application/octet-stream", "Idempotency-Key": "library-photo"}

        self.assertEqual((await self.client.get(places)).status_code, 401)
        self.assertEqual((await self.client.post(gallery, content=raw)).status_code, 401)
        bad_place = await self.client.post(f"{places}/1000000_300000/photos", content=raw, headers=headers)
        self.assertEqual((bad_place.status_code, bad_place.json()["error"]["code"]), (422, "invalid_photo_place"))
        invalid = await self.client.post(gallery, content=b"<svg/>", headers=headers)
        self.assertEqual((invalid.status_code, invalid.json()["error"]["code"]), (422, "invalid_campus_photo"))
        uploaded = await self.client.post(gallery, content=raw, headers=headers)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        first = uploaded.json()
        self.assertEqual(first["status"], "pending")
        retry = await self.client.post(gallery, content=raw, headers=headers)
        self.assertEqual(retry.json(), first)
        changed = await self.client.post(gallery, content=make_photo("orange"), headers=headers)
        self.assertEqual(changed.status_code, 409)
        url = f"/v1/campus-view/photos/{first['id']}/image"
        self.assertEqual((await self.client.get(url, headers=viewer)).status_code, 404)
        self.assertEqual((await self.client.get(places, headers=viewer)).json()["items"], [])
        self.assertEqual((await self.client.get(gallery, headers=viewer)).json()["items"], [])

        pending = await _execute(self.app_dsn, "photo_reviews", {"status": "pending", "offset": 0})
        self.assertEqual([str(row["id"]) for row in pending["items"]], [first["id"]])
        review = {"actor": "web:" + operator, "id": first["id"], "payload": {"version": 1, "status": "approved"}}
        result = await _execute(self.app_dsn, "photo_review", review)
        self.assertEqual(result["version"], 2)
        with self.assertRaises(ServiceError) as stale:
            await _execute(self.app_dsn, "photo_review", review)
        self.assertEqual(stale.exception.status, 409)

        image = await self.client.get(url, headers=viewer)
        self.assertEqual((image.status_code, image.headers["content-type"]), (200, "image/jpeg"))
        self.assertNotIn(b"private source metadata", image.content)
        with Image.open(BytesIO(image.content)) as decoded:
            self.assertEqual(decoded.size, (160, 110))
            self.assertFalse(decoded.getexif())
        self.assertEqual((await self.client.get(url)).status_code, 401)
        second = (await self.client.post(gallery, content=make_photo("green"), headers={
            **viewer, "Content-Type": "application/octet-stream", "Idempotency-Key": "second",
        })).json()
        await _execute(self.app_dsn, "photo_review", {**review, "id": second["id"]})
        listing = (await self.client.get(gallery, headers=viewer)).json()["items"]
        self.assertEqual({row["id"] for row in listing}, {first["id"], second["id"]})
        self.assertTrue(all(set(row) == {"id", "created_at"} for row in listing))
        self.assertEqual((await self.client.get(places, headers=viewer)).json()["items"], [
            {"place": location, "photo_count": 2},
        ])
        await _execute(self.app_dsn, "photo_review", {**review, "payload": {"version": 2, "status": "rejected"}})
        self.assertEqual((await self.client.get(url, headers=viewer)).status_code, 404)
        self.assertEqual([row["id"] for row in (await self.client.get(gallery, headers=viewer)).json()["items"]], [second["id"]])

        other_place = "1105012_355003"
        other = await self.client.post(f"{places}/{other_place}/photos", content=raw, headers={
            **headers, "Idempotency-Key": "other-place",
        })
        self.assertEqual(other.status_code, 201, other.text)
        grouped = await _execute(self.app_dsn, "photo_review_places", {"status": "all", "offset": 0})
        self.assertEqual(grouped["items"], [
            {"place": location, "name": "图书馆", "photo_count": 2},
            {"place": other_place, "name": "阅览室", "photo_count": 1},
        ])
        filtered = await _execute(self.app_dsn, "photo_reviews", {"status": "rejected", "offset": 0, "place": location})
        self.assertEqual([str(row["id"]) for row in filtered["items"]], [first["id"]])

        deletion = {"actor": "web:" + operator, "id": second["id"], "version": 1}
        with self.assertRaises(ServiceError) as stale_delete:
            await _execute(self.app_dsn, "photo_delete", deletion)
        self.assertEqual(stale_delete.exception.status, 409)
        self.assertEqual([row["id"] for row in (await self.client.get(gallery, headers=viewer)).json()["items"]], [second["id"]])
        await _execute(self.app_dsn, "photo_delete", {**deletion, "version": 2})
        await _execute(self.app_dsn, "photo_delete", {**deletion, "id": first["id"], "version": 3})
        with self.assertRaises(ServiceError) as missing:
            await _execute(self.app_dsn, "photo_preview", {"id": second["id"]})
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual((await self.client.get(f"/v1/campus-view/photos/{second['id']}/image", headers=viewer)).status_code, 404)
        self.assertEqual((await self.client.get(places, headers=viewer)).json()["items"], [])
        self.assertEqual((await self.client.get(gallery, headers=viewer)).json()["items"], [])
        remaining = await _execute(self.app_dsn, "photo_review_places", {"status": "all", "offset": 0})
        self.assertEqual([row["place"] for row in remaining["items"]], [other_place])
