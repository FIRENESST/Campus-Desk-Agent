"""Expose only the campus digest's WeRead session and active scan state."""

import base64
import json
import re
from datetime import datetime
from threading import Lock, Thread
from time import monotonic

from .weread_poll import WEREAD_POLL_SECONDS


SHELF = "https://weread.qq.com/web/shelf/sync?userVid=&synckey=0"
SCAN_PHASES = {"idle", "waiting_scan", "scanned", "success", "failed"}
SESSION_STATES = {"checking", "online", "needs_login", "unknown"}
MAX_QR_BASE64 = 128 * 1024


class WeReadAdmin:
    def __init__(self, accounts, cookies, shelf, login_status, start_login):
        self.accounts = accounts
        self.cookies = cookies
        self.shelf = shelf
        self.login_status = login_status
        self.start_login = start_login
        self._lock = Lock()
        self._checking = False
        self._verified = None
        self._recheck_at = 0.0
        self._state = "checking"

    def _verify(self, accounts, fingerprint):
        state = "needs_login"
        try:
            for _, jar in accounts:
                try:
                    if not self.cookies(jar).get("wr_skey"):
                        continue
                    result = json.loads(self.shelf(jar, SHELF))
                except (OSError, TypeError, ValueError):
                    result = {}
                if isinstance(result, dict) and isinstance(result.get("books"), list):
                    state = "online"
                    break
                if not isinstance(result, dict) or result.get("errCode") not in (-2010, -2012):
                    state = "unknown"
        finally:
            with self._lock:
                self._state, self._verified = state, fingerprint
                self._recheck_at = monotonic() + WEREAD_POLL_SECONDS
                self._checking = False

    def _session(self):
        try:
            accounts = self.accounts()
            fingerprint = tuple((name, jar, jar.stat().st_mtime_ns) for name, jar in accounts)
        except OSError:
            return "unknown"
        if not accounts:
            return "needs_login"
        with self._lock:
            changed = fingerprint != self._verified
            if not self._checking and (changed or monotonic() >= self._recheck_at):
                self._checking = True
                Thread(target=self._verify, args=(accounts, fingerprint), daemon=True).start()
            return "checking" if changed else self._state

    def status(self, *, include_qr=False):
        scan = self.login_status()
        phase = scan.get("state")
        if phase in {"waiting_scan", "scanned"} and scan.get("expires_at"):
            try:
                if datetime.fromisoformat(scan["expires_at"]).timestamp() <= datetime.now().timestamp():
                    phase = "failed"
            except (TypeError, ValueError):
                pass
        data = {"session": self._session(), "phase": phase if phase in SCAN_PHASES else "idle"}
        if include_qr and phase == "waiting_scan":
            encoded = scan.get("qr_png_base64")
            if isinstance(encoded, str) and 0 < len(encoded) <= MAX_QR_BASE64:
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError:
                    raw = b""
                if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                    data["qr_png_base64"] = encoded
        return data

    def start(self):
        status = self.status(include_qr=True)
        if status["phase"] in {"waiting_scan", "scanned"}:
            return status
        # A named pool takes precedence over the single-account jar in the digest.
        name = next((name for name, jar in self.accounts()
                     if jar.name != "cj.txt" and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name)), "")
        self.start_login(name)
        return self.status(include_qr=True)
