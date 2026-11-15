import base64
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.CampusDigest.weread_admin import WeReadAdmin


class WeReadAdminTests(unittest.TestCase):
    def test_online_session_can_start_a_scan_and_reopen_the_same_code(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        scan = {"state": "idle"}
        start_login = Mock(side_effect=lambda name: scan.update(state="waiting_scan", qr_png_base64=encoded))
        admin = WeReadAdmin(lambda: [("campus", Path("accounts/campus.txt"))],
                            None, None, lambda: scan, start_login)

        with patch.object(admin, "_session", return_value="online"):
            self.assertEqual(admin.status(), {"session": "online", "phase": "idle"})
            pending = {"session": "online", "phase": "waiting_scan", "qr_png_base64": encoded}
            self.assertEqual(admin.start(), pending)
            self.assertEqual(admin.start(), pending)
            self.assertEqual(admin.status(), {"session": "online", "phase": "waiting_scan"})
            start_login.assert_called_once_with("campus")

            scan["state"] = "scanned"
            self.assertEqual(admin.start(), {"session": "online", "phase": "scanned"})
            start_login.assert_called_once_with("campus")
