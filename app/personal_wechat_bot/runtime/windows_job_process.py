from __future__ import annotations

import math
import os
import subprocess
import threading
from collections.abc import Sequence


_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


class WindowsJobProcess:
    """A process created suspended and bound to a kill-on-close Job Object."""

    def __init__(self, *, pid: int, process_handle: int, job_handle: int, argv: Sequence[str]):
        self.pid = int(pid)
        self._process_handle = int(process_handle)
        self._job_handle = int(job_handle)
        self._argv = tuple(str(item) for item in argv)
        self._guard = threading.Lock()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        wait_result = _kernel32().WaitForSingleObject(self._process_handle, 0)
        if wait_result == _WAIT_TIMEOUT:
            return None
        if wait_result != _WAIT_OBJECT_0:
            raise OSError("WaitForSingleObject failed for browser process")
        self.returncode = _process_exit_code(self._process_handle)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            timeout_ms = _INFINITE
        else:
            timeout_ms = max(0, min(_INFINITE - 1, int(math.ceil(float(timeout) * 1000.0))))
        wait_result = _kernel32().WaitForSingleObject(self._process_handle, timeout_ms)
        if wait_result == _WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self._argv, timeout)
        if wait_result != _WAIT_OBJECT_0:
            raise OSError("WaitForSingleObject failed for browser process")
        self.returncode = _process_exit_code(self._process_handle)
        return self.returncode

    def terminate(self) -> None:
        self._terminate_job()

    def kill(self) -> None:
        self._terminate_job()

    def close(self) -> None:
        with self._guard:
            process_handle = self._process_handle
            job_handle = self._job_handle
            self._process_handle = 0
            self._job_handle = 0
        kernel32 = _kernel32()
        if job_handle:
            kernel32.CloseHandle(job_handle)
        if process_handle:
            kernel32.CloseHandle(process_handle)

    def _terminate_job(self) -> None:
        with self._guard:
            job_handle = self._job_handle
            process_handle = self._process_handle
        kernel32 = _kernel32()
        if job_handle:
            if not kernel32.TerminateJobObject(job_handle, 1):
                raise OSError("TerminateJobObject failed for browser process")
            return
        if process_handle and not kernel32.TerminateProcess(process_handle, 1):
            raise OSError("TerminateProcess failed for browser process")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def start_windows_job_process(argv: Sequence[str]) -> WindowsJobProcess:
    if os.name != "nt":
        raise RuntimeError("Windows Job Process is only available on Windows")
    if not argv:
        raise ValueError("process argv cannot be empty")
    import ctypes
    from ctypes import wintypes

    class _StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    kernel32 = _kernel32()
    job_handle = _create_kill_on_close_job()
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(startup)
    process_info = _ProcessInformation()
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(item) for item in argv]))
    created = kernel32.CreateProcessW(
        str(argv[0]),
        command_line,
        None,
        None,
        False,
        _CREATE_SUSPENDED,
        None,
        None,
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    if not created:
        kernel32.CloseHandle(job_handle)
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel32.AssignProcessToJobObject(job_handle, process_info.hProcess):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel32.ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        process = WindowsJobProcess(
            pid=int(process_info.dwProcessId),
            process_handle=int(process_info.hProcess),
            job_handle=int(job_handle),
            argv=argv,
        )
        process_info.hProcess = None
        job_handle = 0
        return process
    except BaseException:
        kernel32.TerminateProcess(process_info.hProcess, 1)
        raise
    finally:
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        if job_handle:
            kernel32.CloseHandle(job_handle)


def _create_kill_on_close_job() -> int:
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = _kernel32()
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return int(handle)


def _process_exit_code(process_handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    exit_code = wintypes.DWORD()
    if not _kernel32().GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        raise OSError("GetExitCodeProcess failed for browser process")
    return int(exit_code.value)


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
