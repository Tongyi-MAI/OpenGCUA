"""Read-only Windows identity checks and ownership of the driver process.

All desktop mutations go through CUA. Process creation times prevent PID reuse
from reviving old bindings; inaccessible/elevated targets are never admitted.
"""

import ctypes
import hashlib
import os
import tempfile
from ctypes import wintypes

from transport import Error

_worker_job = None


def contain_worker():
    """Assign this worker before spawning children, closing the creation race.

    The handle stays live until process teardown. A normal close first stops the
    driver; an abrupt worker exit closes the same non-inherited handle in the
    kernel and terminates its descendants. Never call this in a user's host.
    """
    global _worker_job
    if _worker_job is None:

        class CurrentProcess:
            _handle = -1  # GetCurrentProcess pseudo-handle, not an owned handle.

        _worker_job = Job(CurrentProcess())


class Native:
    def __init__(self):
        if os.name != "nt":
            raise Error("UNSUPPORTED_CAPABILITY", "Windows provider requires a native Windows host")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        self.user.GetThreadDesktop.restype = wintypes.HANDLE
        self.user.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.user.OpenInputDesktop.restype = wintypes.HANDLE
        self.user.CloseDesktop.argtypes = [wintypes.HANDLE]
        session = wintypes.DWORD()
        self.kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
        desktop = self.user.GetThreadDesktop(self.kernel.GetCurrentThreadId())
        name, length = ctypes.create_unicode_buffer(256), wintypes.DWORD()
        if not desktop or not self.user.GetUserObjectInformationW(
            desktop, 2, name, ctypes.sizeof(name), ctypes.byref(length)
        ):
            raise Error("PERMISSION_DENIED", "Cannot identify the interactive Windows desktop")
        if name.value.casefold() != "default":
            raise Error("PERMISSION_DENIED", "Only the normal user desktop is supported")
        self.desktop_id = f"windows:cua:session:{session.value}:desktop:{name.value.casefold()}"
        self.lease = None

    def check_desktop(self):
        desktop = self.user.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            raise Error("PERMISSION_DENIED", "The interactive desktop is locked or unavailable")
        try:
            name, length = ctypes.create_unicode_buffer(256), wintypes.DWORD()
            if (
                not self.user.GetUserObjectInformationW(
                    desktop, 2, name, ctypes.sizeof(name), ctypes.byref(length)
                )
                or name.value.casefold() != "default"
            ):
                raise Error(
                    "PERMISSION_DENIED",
                    "Secure desktops and system permission prompts are outside provider scope",
                )
        finally:
            self.user.CloseDesktop(desktop)

    def acquire(self):
        import msvcrt

        key = hashlib.sha256(self.desktop_id.encode()).hexdigest()[:24]
        path = os.path.join(tempfile.gettempdir(), f"opengcua-windows-{key}.lock")
        lease = open(path, "a+b")
        try:
            lease.seek(0)
            if not lease.read(1):
                lease.write(b"\0")
                lease.flush()
            lease.seek(0)
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            lease.close()
            raise Error(
                "RESOURCE_BUSY", "Another OpenGCUA Windows provider owns this desktop"
            ) from exc
        self.lease = lease

    def close(self):
        if self.lease:
            self.lease.close()
            self.lease = None

    def process(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                raise Error(
                    "PERMISSION_DENIED",
                    "Target process cannot be inspected at the current privilege level",
                )
            raise Error("STALE_TARGET", "Target process ended or cannot be inspected")
        token = wintypes.HANDLE()
        try:
            if not self.advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
                raise Error("PERMISSION_DENIED", "Cannot verify the target process privilege level")
            elevated, length = wintypes.DWORD(), wintypes.DWORD()
            if not self.advapi.GetTokenInformation(
                token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(length)
            ):
                raise Error("PERMISSION_DENIED", "Cannot verify the target process privilege level")
            if elevated.value:
                raise Error(
                    "PERMISSION_DENIED", "Elevated applications are outside Windows provider scope"
                )
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise Error("STALE_TARGET", "Cannot verify target process creation time")
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            if token:
                self.kernel.CloseHandle(token)
            self.kernel.CloseHandle(handle)

    def window(self, hwnd, pid):
        owner = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if not thread or owner.value != pid:
            raise Error("STALE_TARGET", "Window closed or no longer belongs to the bound process")
        return thread

    def foreground(self):
        point = wintypes.POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise Error("PERMISSION_DENIED", "Interactive desktop is unavailable")
        return (int(self.user.GetForegroundWindow() or 0), point.x, point.y)


class Job:
    """Kill only the owned driver and descendants when the provider exits."""

    def __init__(self, process):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]

        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: basic limits (64 bytes on
        # x64), IO_COUNTERS (48), four SIZE_T values. Use declared structs so
        # the same code also works on ARM64 and 32-bit Python.
        class Basic(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong),
                ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        info = Extended()
        info.basic.flags = 0x2000
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "Cannot create driver process job")
        if not kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel.AssignProcessToJobObject(
            self.handle, wintypes.HANDLE(int(process._handle))
        ):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "Cannot isolate driver process lifetime")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
