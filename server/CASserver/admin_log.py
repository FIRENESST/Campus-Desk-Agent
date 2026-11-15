"""Recent CAS events available to an authenticated local administrator."""

import logging
from collections import deque
from datetime import datetime
from threading import Lock


class CASAdminLog:
    def __init__(self):
        self._rows = deque(maxlen=120)
        self._lock = Lock()
        self.record("CAS 身份服务已启动。")

    def record(self, message, level="info"):
        # Callers supply fixed messages. Request data, tickets and credentials stay out of the log.
        with self._lock:
            self._rows.append({"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                               "level": level, "message": message})
        logging.getLogger("uvicorn.error").log(getattr(logging, level.upper()), message)

    def recent(self):
        with self._lock:
            return list(self._rows)
