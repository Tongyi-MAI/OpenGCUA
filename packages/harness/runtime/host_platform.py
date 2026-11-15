"""Host process and private-file primitives; no desktop automation lives here."""
from __future__ import annotations

import os
import sys
from pathlib import Path


IS_WINDOWS = sys.platform == "win32"


def _windows_api():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel


class WindowsJob:
    """Own a process tree without killing processes by a reusable PID later.

    Attach before releasing the worker's startup handshake. Closing the final
    job handle kills all descendants, including children of an exited worker.
    The handle is never inherited by those descendants.
    """

    def __init__(self, pid: int):
        ctypes, wintypes, kernel = _windows_api()

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io_counters", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        self._kernel = kernel
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        process_handle = None
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process_handle:
                kernel.CloseHandle(process_handle)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._kernel.CloseHandle(handle)


def open_private_file(path: Path, *, create: bool = True, directory: bool = False) -> int:
    """Open an owned file/directory with a private ACL/mode, without symlinks.

    Files are opened for append/read. Windows directories use inheritable ACLs
    so SQLite sidecars and other subsequently created files remain private.
    """
    if not IS_WINDOWS:
        if directory:
            raise ValueError("Directory handles are only used on Windows")
        fd = os.open(path, os.O_RDWR | os.O_APPEND | (os.O_CREAT if create else 0)
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    import msvcrt

    ctypes, wintypes, kernel = _windows_api()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    token = wintypes.HANDLE()
    descriptor = ctypes.c_void_p()
    sid_string = wintypes.LPWSTR()
    handle = None
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.GetTokenInformation.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    )
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    )
    advapi.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    )
    advapi.SetSecurityInfo.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    )
    advapi.SetSecurityInfo.restype = wintypes.DWORD
    advapi.GetSecurityInfo.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    )
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                    ("inherit_handle", wintypes.BOOL)]

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes))
    kernel.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f"D:P(A;{'OICI' if directory else ''};FA;;;{sid_string.value})", 1,
            ctypes.byref(descriptor), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        if directory and create and not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS
                raise ctypes.WinError(error)
        handle = kernel.CreateFileW(
            str(path), 0xC0040000, 0x00000003, ctypes.byref(attributes),
            4 if create and not directory else 3,  # OPEN_ALWAYS / OPEN_EXISTING
            0x00200080 | (0x02000000 if directory else 0), None,
        )
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        tag = AttributeTag()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise ctypes.WinError(ctypes.get_last_error())
        if tag.attributes & 0x400 or bool(tag.attributes & 0x10) != directory:
            raise OSError("Private storage cannot use links or an unexpected file type")
        info = FileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not directory and info.links != 1:
            raise OSError("Private storage cannot use a multiply linked file")
        owner, current_descriptor = ctypes.c_void_p(), ctypes.c_void_p()
        error = advapi.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None,
                                      ctypes.byref(current_descriptor))
        if error:
            raise ctypes.WinError(error)
        try:
            if not advapi.EqualSid(owner, sid):
                raise PermissionError("Private storage is owned by a different account")
        finally:
            kernel.LocalFree(current_descriptor)
        dacl, present, defaulted = ctypes.c_void_p(), wintypes.BOOL(), wintypes.BOOL()
        if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
        fd = msvcrt.open_osfhandle(handle, (os.O_RDONLY if directory else os.O_RDWR | os.O_APPEND) | os.O_BINARY)
        handle = None  # CRT now owns it
        return fd
    finally:
        if handle:
            kernel.CloseHandle(handle)
        if token:
            kernel.CloseHandle(token)
        if sid_string:
            kernel.LocalFree(sid_string)
        if descriptor:
            kernel.LocalFree(descriptor)


def sync_parent_directory(path: Path) -> None:
    # Windows CRT cannot open directories. The journal file itself is flushed
    # with os.fsync (FlushFileBuffers); do not silently weaken POSIX durability.
    if not IS_WINDOWS:
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def windows_pipe_available(fd: int) -> int | None:
    """Return readable bytes, or None for EOF, without waiting on a pipe."""
    import msvcrt
    ctypes, wintypes, kernel = _windows_api()
    kernel.PeekNamedPipe.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    )
    available = wintypes.DWORD()
    if not kernel.PeekNamedPipe(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error in (109, 232):  # ERROR_BROKEN_PIPE / ERROR_NO_DATA
            return None
        raise ctypes.WinError(error)
    return available.value


def windows_standard_handle(fd: int) -> None:
    """Keep native child stdio aligned with the CRT descriptors being captured."""
    import msvcrt
    ctypes, wintypes, kernel = _windows_api()
    kernel.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
    if not kernel.SetStdHandle(-11 if fd == 1 else -12, msvcrt.get_osfhandle(fd)):
        raise ctypes.WinError(ctypes.get_last_error())


def user_data_directory() -> Path:
    """Return the host's per-user data location without creating it."""
    if base := os.environ.get("GENERALIST_DATA_DIR"):
        return Path(base).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData/Local") / "Generalist"
    return Path.home() / "Library/Application Support/Generalist"
