"""Check one gateway request with the local process manager."""

import json
import os
import re
import time


LOCAL_AUTH_PIPE = re.compile(r"\\\\\.\\pipe\\mpm-auth-[0-9a-f]{32}\Z")
MAX_MESSAGE = 1024
MAX_RUNTIME_RESPONSE = 64 * 1024


class RequestAuthorizationUnavailable(RuntimeError):
    """The supervisor did not return a verified authorization result."""


def _connect(address: str, expected_pid: int):
    import _winapi as win
    import ctypes
    from ctypes import wintypes
    from multiprocessing.connection import PipeConnection

    deadline = time.monotonic() + 2
    while True:
        try:
            handle = win.CreateFile(
                address, win.GENERIC_READ | win.GENERIC_WRITE, 0, win.NULL,
                win.OPEN_EXISTING, win.FILE_FLAG_OVERLAPPED, win.NULL,
            )
            break
        except OSError as exc:
            if exc.winerror != win.ERROR_PIPE_BUSY:
                raise
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining < 1:
                raise TimeoutError("主管理器鉴权通道忙") from None
            win.WaitNamedPipe(address, remaining)
    try:
        server_pid = ctypes.WinDLL("kernel32", use_last_error=True).GetNamedPipeServerProcessId
        server_pid.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        server_pid.restype = wintypes.BOOL
        pid = wintypes.ULONG()
        if not server_pid(handle, ctypes.byref(pid)) or pid.value != expected_pid:
            raise RequestAuthorizationUnavailable("无法确认本机主管理器进程")
        win.SetNamedPipeHandleState(handle, win.PIPE_READMODE_MESSAGE, None, None)
        return PipeConnection(handle)
    except BaseException:
        win.CloseHandle(handle)
        raise


def _exchange(address, supervisor_pid, request, response_limit):
    if os.name != "nt" or not LOCAL_AUTH_PIPE.fullmatch(address):
        raise RequestAuthorizationUnavailable("当前进程未连接本机主管理器")
    payload = json.dumps(request, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_MESSAGE:
        raise RequestAuthorizationUnavailable("接口鉴权请求过大")
    try:
        with _connect(address, supervisor_pid) as pipe:
            pipe.send_bytes(payload)
            if not pipe.poll(4):
                raise TimeoutError
            response = pipe.recv_bytes(response_limit)
            try:
                pipe.send_bytes(b"\x01")
            except OSError:
                pass
        result = json.loads(response)
        if not isinstance(result, dict) or type(result.get("allowed")) is not bool:
            raise ValueError
        return result
    except (OSError, EOFError, ValueError):
        raise RequestAuthorizationUnavailable("主管理器未返回有效的鉴权结果") from None


def authorize_request(address: str, supervisor_pid: int, method: str, route: str) -> bool:
    result = _exchange(address, supervisor_pid, {"method": method, "route": route}, MAX_MESSAGE)
    if result.keys() != {"allowed"}:
        raise RequestAuthorizationUnavailable("主管理器未返回有效的鉴权结果")
    return result["allowed"]


def runtime_request(address: str, supervisor_pid: int, request: dict) -> dict:
    return _exchange(address, supervisor_pid, request, MAX_RUNTIME_RESPONSE)
