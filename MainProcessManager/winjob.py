"""Launch a process directly into a Windows Job Object and retain its handles."""

import _winapi
import ctypes as ct
import msvcrt
import os
import signal
import subprocess
import time
from contextlib import contextmanager
from ctypes import wintypes as w
from weakref import finalize


KERNEL = ct.WinDLL("kernel32", use_last_error=True)
USER = ct.WinDLL("user32", use_last_error=True)
VOID_P = ct.c_void_p
SIZE_T = ct.c_size_t
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_BASIC_ACCOUNTING = 1
JOB_PROCESS_IDS = 3
JOB_EXTENDED_LIMITS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
TCP_TABLE_OWNER_PID_LISTENER = 3
WM_CLOSE = 0x0010


class Tcp4Listener(ct.Structure):
    _fields_ = [(field, w.DWORD) for field in
                ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]


class Tcp6Listener(ct.Structure):
    _fields_ = [
        ("local_addr", ct.c_ubyte * 16), ("local_scope", w.DWORD), ("local_port", w.DWORD),
        ("remote_addr", ct.c_ubyte * 16), ("remote_scope", w.DWORD), ("remote_port", w.DWORD),
        ("state", w.DWORD), ("pid", w.DWORD),
    ]


class BasicLimits(ct.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ct.c_int64),
        ("PerJobUserTimeLimit", ct.c_int64),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class ExtendedLimits(ct.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits),
        ("IoInfo", ct.c_uint64 * 6),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class Accounting(ct.Structure):
    _fields_ = [
        ("TotalUserTime", ct.c_int64),
        ("TotalKernelTime", ct.c_int64),
        ("ThisPeriodTotalUserTime", ct.c_int64),
        ("ThisPeriodTotalKernelTime", ct.c_int64),
        ("TotalPageFaultCount", w.DWORD),
        ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD),
        ("TotalTerminatedProcesses", w.DWORD),
    ]


class StartupInfo(ct.Structure):
    _fields_ = [
        ("cb", w.DWORD),
        ("lpReserved", w.LPWSTR),
        ("lpDesktop", w.LPWSTR),
        ("lpTitle", w.LPWSTR),
        ("dwX", w.DWORD),
        ("dwY", w.DWORD),
        ("dwXSize", w.DWORD),
        ("dwYSize", w.DWORD),
        ("dwXCountChars", w.DWORD),
        ("dwYCountChars", w.DWORD),
        ("dwFillAttribute", w.DWORD),
        ("dwFlags", w.DWORD),
        ("wShowWindow", w.WORD),
        ("cbReserved2", w.WORD),
        ("lpReserved2", VOID_P),
        ("hStdInput", w.HANDLE),
        ("hStdOutput", w.HANDLE),
        ("hStdError", w.HANDLE),
    ]


class ExtendedStartupInfo(ct.Structure):
    _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", VOID_P)]


class ProcessInformation(ct.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


def _bind(name, *parameters, result=w.BOOL):
    fn = getattr(KERNEL, name)
    fn.argtypes, fn.restype = parameters, result
    return fn


create_job = _bind("CreateJobObjectW", VOID_P, w.LPCWSTR, result=w.HANDLE)
set_job = _bind("SetInformationJobObject", w.HANDLE, ct.c_int, VOID_P, w.DWORD)
query_job = _bind("QueryInformationJobObject", w.HANDLE, ct.c_int, VOID_P, w.DWORD, VOID_P)
terminate_job = _bind("TerminateJobObject", w.HANDLE, w.UINT)
is_in_job = _bind("IsProcessInJob", w.HANDLE, w.HANDLE, ct.POINTER(w.BOOL))
init_attributes = _bind("InitializeProcThreadAttributeList", VOID_P, w.DWORD, w.DWORD, ct.POINTER(SIZE_T))
update_attribute = _bind("UpdateProcThreadAttribute", VOID_P, w.DWORD, SIZE_T, VOID_P, SIZE_T, VOID_P, VOID_P)
delete_attributes = _bind("DeleteProcThreadAttributeList", VOID_P, result=None)
create_process = _bind(
    "CreateProcessW", w.LPCWSTR, w.LPWSTR, VOID_P, VOID_P, w.BOOL, w.DWORD, VOID_P, w.LPCWSTR,
    ct.POINTER(ExtendedStartupInfo), ct.POINTER(ProcessInformation),
)
console_break = _bind("GenerateConsoleCtrlEvent", w.DWORD, w.DWORD)
tcp_table = ct.WinDLL("iphlpapi").GetExtendedTcpTable
tcp_table.argtypes = (VOID_P, ct.POINTER(w.DWORD), w.BOOL, w.ULONG, ct.c_int, w.ULONG)
tcp_table.restype = w.DWORD
window_callback = ct.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
USER.EnumWindows.argtypes = (window_callback, w.LPARAM)
USER.EnumWindows.restype = w.BOOL
USER.GetWindowThreadProcessId.argtypes = (w.HWND, ct.POINTER(w.DWORD))
USER.GetWindowThreadProcessId.restype = w.DWORD
USER.IsWindowVisible.argtypes = (w.HWND,)
USER.IsWindowVisible.restype = w.BOOL
USER.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)
USER.PostMessageW.restype = w.BOOL


def _check(success):
    if not success:
        raise ct.WinError(ct.get_last_error())


def _close_job_handles(job, process):
    try:
        _winapi.CloseHandle(job)
    finally:
        _winapi.CloseHandle(process)



def listening_pids() -> set[int]:
    owners = set()
    for family, row_type in ((2, Tcp4Listener), (23, Tcp6Listener)):
        size = w.DWORD()
        buffer = None
        for _ in range(5):
            error = tcp_table(buffer, ct.byref(size), False, family, TCP_TABLE_OWNER_PID_LISTENER, 0)
            if error != ERROR_INSUFFICIENT_BUFFER:
                break
            buffer = ct.create_string_buffer(size.value)
        if error == 50 and family == 23:  # IPv6 table is unavailable on this machine.
            continue
        if error:
            raise ct.WinError(error)
        count = w.DWORD.from_buffer(buffer).value
        rows = (row_type * count).from_buffer(buffer, ct.sizeof(w.DWORD))
        owners.update(row.pid for row in rows)
    return owners


@contextmanager
def _attributes(input_handle, log_handle, job_handle):
    size = SIZE_T()
    init_attributes(None, 2, 0, ct.byref(size))
    _check(size.value)
    buffer = ct.create_string_buffer(size.value)
    _check(init_attributes(buffer, 2, 0, ct.byref(size)))
    try:
        handles = (w.HANDLE * 2)(input_handle, log_handle)
        jobs = (w.HANDLE * 1)(job_handle)
        _check(update_attribute(buffer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles, ct.sizeof(handles), None, None))
        _check(update_attribute(buffer, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs, ct.sizeof(jobs), None, None))
        yield ct.cast(buffer, VOID_P)
    finally:
        delete_attributes(buffer)


class ProcessJob:
    def __init__(self, job_handle, process_handle, pid):
        self._job = job_handle
        self._process = process_handle
        self.pid = pid
        self._finalizer = finalize(self, _close_job_handles, job_handle, process_handle)

    @classmethod
    def start(cls, argv, cwd, env, log_path):
        job_handle = create_job(None, None)
        _check(job_handle)
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            _check(set_job(job_handle, JOB_EXTENDED_LIMITS, ct.byref(limits), ct.sizeof(limits)))
            command = ct.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment = ct.create_unicode_buffer("\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.upper)) + "\0\0")
            info = ProcessInformation()
            with open(os.devnull, "rb") as stdin, log_path.open("ab", buffering=0) as output:
                input_handle = msvcrt.get_osfhandle(stdin.fileno())
                log_handle = msvcrt.get_osfhandle(output.fileno())
                os.set_handle_inheritable(input_handle, True)
                os.set_handle_inheritable(log_handle, True)
                with _attributes(input_handle, log_handle, job_handle) as attributes:
                    startup = ExtendedStartupInfo(lpAttributeList=attributes)
                    startup.StartupInfo.cb = ct.sizeof(startup)
                    startup.StartupInfo.dwFlags = subprocess.STARTF_USESTDHANDLES
                    startup.StartupInfo.hStdInput = input_handle
                    startup.StartupInfo.hStdOutput = log_handle
                    startup.StartupInfo.hStdError = log_handle
                    flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | subprocess.CREATE_NEW_PROCESS_GROUP
                    _check(create_process(argv[0], command, None, None, True, flags, environment, str(cwd),
                                          ct.byref(startup), ct.byref(info)))
            _winapi.CloseHandle(info.hThread)
            return cls(job_handle, info.hProcess, info.dwProcessId)
        except BaseException:
            _winapi.CloseHandle(job_handle)
            raise

    def active_count(self):
        accounting = Accounting()
        _check(query_job(self._job, JOB_BASIC_ACCOUNTING, ct.byref(accounting), ct.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def pids(self) -> set[int]:
        header_bytes = 2 * ct.sizeof(w.DWORD)
        capacity = max(16, self.active_count())
        for _ in range(8):
            buffer = ct.create_string_buffer(header_bytes + capacity * ct.sizeof(SIZE_T))
            success = query_job(self._job, JOB_PROCESS_IDS, buffer, ct.sizeof(buffer), None)
            if success:
                count = w.DWORD.from_buffer(buffer, ct.sizeof(w.DWORD)).value
                return set((SIZE_T * count).from_buffer(buffer, header_bytes))
            if ct.get_last_error() != ERROR_MORE_DATA:
                _check(False)
            capacity *= 2
        raise OSError("进程组成员变动过快")

    def contains(self, pid: int):
        try:
            handle = _winapi.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except OSError:
            return False
        try:
            member = w.BOOL()
            _check(is_in_job(handle, self._job, ct.byref(member)))
            return bool(member.value)
        finally:
            _winapi.CloseHandle(handle)

    def request_window_close(self):
        members = self.pids()
        sent = False

        @window_callback
        def close_owned(hwnd, _):
            nonlocal sent
            pid = w.DWORD()
            USER.GetWindowThreadProcessId(hwnd, ct.byref(pid))
            if pid.value in members and USER.IsWindowVisible(hwnd) and self.contains(pid.value):
                if USER.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                    sent = True
            return True

        _check(USER.EnumWindows(close_owned, 0))
        return sent

    def wait_root(self, timeout: float = 0) -> int | None:
        if _winapi.WaitForSingleObject(self._process, int(timeout * 1000)) == _winapi.WAIT_TIMEOUT:
            return None
        return _winapi.GetExitCodeProcess(self._process)

    def wait_empty(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while self.active_count():
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)
        return True

    def stop(self):
        if self.wait_root() is None and console_break(signal.CTRL_BREAK_EVENT, self.pid):
            if self.wait_empty(8):
                return
        if self.active_count():
            _check(terminate_job(self._job, 1))
        if not self.wait_empty(5):
            raise RuntimeError("Windows 进程组仍在退出")

    def close(self):
        self._finalizer()
