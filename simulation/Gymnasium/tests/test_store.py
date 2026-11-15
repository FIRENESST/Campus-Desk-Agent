import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from store import GymError, GymStore
from fixtures import gym_store


class GymStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = gym_store()
        self.addCleanup(self.store.close)

    def assert_error(self, code, callable_, *args):
        with self.assertRaises(GymError) as caught:
            callable_(*args)
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(caught.exception.message)
        self.assertIsInstance(caught.exception.retryable, bool)
        return caught.exception

    def test_list_venues_exposes_capacity_and_remaining(self):
        venues = self.store.list_venues()

        self.assertGreaterEqual(len(venues), 3)
        strength = next(venue for venue in venues if venue["id"] == "strength")
        self.assertEqual(2, strength["capacity"])
        self.assertEqual(2, strength["remaining"])
        self.assertEqual("测试场馆 1", strength["name"])

    def test_reservation_updates_venue_and_reservation_views(self):
        reservation = self.store.reserve("strength", "  张敏  ")

        self.assertEqual("张敏", reservation["user_name"])
        self.assertEqual("strength", reservation["venue_id"])
        self.assertTrue(reservation["reservation_id"])
        self.assertEqual([reservation], self.store.list_reservations())
        strength = next(v for v in self.store.list_venues() if v["id"] == "strength")
        self.assertEqual(1, strength["remaining"])

    def test_duplicate_reservation_is_conflict_without_partial_write(self):
        self.store.reserve("strength", "张敏")

        self.assert_error("CONFLICT", self.store.reserve, "strength", "张敏")

        self.assertEqual(1, len(self.store.list_reservations()))
        strength = next(v for v in self.store.list_venues() if v["id"] == "strength")
        self.assertEqual(1, strength["remaining"])

    def test_capacity_full_is_stable_error(self):
        self.store.reserve("strength", "用户甲")
        self.store.reserve("strength", "用户乙")

        error = self.assert_error("CAPACITY_FULL", self.store.reserve, "strength", "用户丙")

        self.assertTrue(error.retryable)
        self.assertEqual(2, len(self.store.list_reservations()))

    def test_cancel_restores_capacity_and_unknown_ids_are_not_found(self):
        reservation = self.store.reserve("strength", "张敏")

        cancelled = self.store.cancel(reservation["reservation_id"])

        self.assertEqual(reservation, cancelled)
        self.assertEqual([], self.store.list_reservations())
        strength = next(v for v in self.store.list_venues() if v["id"] == "strength")
        self.assertEqual(2, strength["remaining"])
        self.assert_error("NOT_FOUND", self.store.cancel, reservation["reservation_id"])
        self.assert_error("NOT_FOUND", self.store.reserve, "missing", "张敏")

    def test_invalid_arguments_are_rejected(self):
        invalid_calls = [
            (self.store.reserve, ("", "张敏")),
            (self.store.reserve, ("strength", "  ")),
            (self.store.reserve, (None, "张敏")),
            (self.store.reserve, ("strength", 7)),
            (self.store.cancel, (" ",)),
            (self.store.cancel, (None,)),
        ]
        for callable_, args in invalid_calls:
            with self.subTest(callable=callable_.__name__, args=args):
                self.assert_error("INVALID_ARGUMENT", callable_, *args)
        self.assertEqual([], self.store.list_reservations())

    def test_concurrent_reservations_never_exceed_capacity(self):
        def reserve(index):
            try:
                return self.store.reserve("cardio", f"用户{index}")
            except GymError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(reserve, range(12)))

        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, str)]
        self.assertEqual(3, len(successes))
        self.assertEqual(["CAPACITY_FULL"] * 9, failures)
        self.assertEqual(3, len(self.store.list_reservations()))
        cardio = next(v for v in self.store.list_venues() if v["id"] == "cardio")
        self.assertEqual(0, cardio["remaining"])

    def test_slot_capacity_persists_and_started_slots_cannot_be_rebooked(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "reservations.sqlite3"
            now = datetime.fromisoformat("2026-11-13T08:15+08:00")
            first, second = gym_store(path, clock=lambda: now), gym_store(path, clock=lambda: now)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            venue, day = "indoor-basketball", "2026-11-13"
            slots = first.list_slots(venue, day)
            self.assertEqual(slots[0]["slot_id"], "2026-11-13_1_0900")
            self.assert_error("SLOT_UNAVAILABLE", first.reserve, venue, "甲", "2026-11-13_1_0800")
            with ThreadPoolExecutor(max_workers=2) as executor:
                def reserve(store, user):
                    try:
                        return store.reserve(venue, user, slots[0]["slot_id"])
                    except GymError as error:
                        return error.code
                reservations = list(executor.map(reserve, (first, second), ("甲", "乙")))
            self.assertIn("CAPACITY_FULL", reservations)
            receipt = next(item for item in reservations if isinstance(item, dict))
            first.close()
            second.close()
            self.assertEqual(first.list_reservations(), [receipt])
            self.assertEqual(first.list_slots(venue, day)[0]["remaining"], 0)
            first.close()


if __name__ == "__main__":
    unittest.main()
