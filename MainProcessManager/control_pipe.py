"""Authenticate each local interface request against Windows process identity."""

import _winapi as win
import ctypes
import json
import secrets
from ctypes import wintypes
from threading import Event, Thread

PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
MAX_MESSAGE = 1024
MAX_RESPONSE = 64 * 1024
IO_TIMEOUT_MS = 1500
KERNEL = ctypes.WinDLL("kernel32", use_last_error=True)
client_pid = KERNEL.GetNamedPipeClientProcessId
client_pid.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
client_pid.restype = wintypes.BOOL
disconnect = KERNEL.DisconnectNamedPipe
disconnect.argtypes, disconnect.restype = (wintypes.HANDLE,), wintypes.BOOL


def _finish(overlapped, timeout=IO_TIMEOUT_MS):
    if win.WaitForSingleObject(overlapped.event, timeout) == win.WAIT_TIMEOUT:
        overlapped.cancel()
        overlapped.GetOverlappedResult(True)
        raise TimeoutError("本机管道消息超时")
    return overlapped.GetOverlappedResult(True)


def _read(handle):
    operation, _ = win.ReadFile(handle, MAX_MESSAGE, overlapped=True)
    length, error = _finish(operation)
    if error:
        raise OSError("本机管道消息无效或过大")
    return operation.getbuffer()[:length]


def _write(handle, message):
    data = json.dumps(message, separators=(",", ":")).encode("ascii")
    if len(data) > MAX_RESPONSE:
        raise OSError("本机管道响应过大")
    operation, _ = win.WriteFile(handle, data, overlapped=True)
    length, error = _finish(operation)
    if error or length != len(data):
        raise OSError("本机管道响应未完整传输")


class ControlPipe:
    def __init__(self, authorize, runtime=None):
        self.address = "\\\\.\\pipe\\mpm-auth-" + secrets.token_hex(16)
        self.authorize = authorize
        self.runtime = runtime
        self._stop = Event()
        self._thread = Thread(target=self._run, name="mpm-request-auth", daemon=True)

    def __enter__(self):
        self._handle = win.CreateNamedPipe(
            self.address, win.PIPE_ACCESS_DUPLEX | win.FILE_FLAG_OVERLAPPED | win.FILE_FLAG_FIRST_PIPE_INSTANCE,
            win.PIPE_TYPE_MESSAGE | win.PIPE_READMODE_MESSAGE | PIPE_REJECT_REMOTE_CLIENTS,
            1, MAX_RESPONSE, MAX_MESSAGE, IO_TIMEOUT_MS, win.NULL,
        )
        try:
            self._thread.start()
        except BaseException:
            win.CloseHandle(self._handle)
            raise
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        win.CloseHandle(self._handle)

    def _accept(self):
        operation = win.ConnectNamedPipe(self._handle, overlapped=True)
        while win.WaitForSingleObject(operation.event, 200) == win.WAIT_TIMEOUT:
            if self._stop.is_set():
                operation.cancel()
                operation.GetOverlappedResult(True)
                return False
        _, error = operation.GetOverlappedResult(True)
        return error in (0, win.ERROR_PIPE_CONNECTED) and not self._stop.is_set()

    def _response(self, payload):
        try:
            request = json.loads(payload)
        except (ValueError, RecursionError):
            return {"allowed": False}
        pid = wintypes.ULONG()
        if not isinstance(request, dict) or not client_pid(self._handle, ctypes.byref(pid)):
            return {"allowed": False}
        if request.keys() == {"method", "route"}:
            method, route = request["method"], request["route"]
            valid = isinstance(method, str) and bool(method) and isinstance(route, str) and route.startswith("/")
            return {"allowed": valid and self.authorize(pid.value)}
        if self.runtime is not None and "operation" in request:
            try:
                return self.runtime(pid.value, request)
            except (OSError, RuntimeError, ValueError):
                return {"allowed": False}
        return {"allowed": False}

    def _run(self):
        while not self._stop.is_set():
            try:
                if self._accept():
                    payload = _read(self._handle)
                    _write(self._handle, self._response(payload))
                    # Allow the client to consume the response before the next connection.
                    _read(self._handle)
            except (OSError, EOFError):
                self._stop.wait(0.05)
            finally:
                disconnect(self._handle)
