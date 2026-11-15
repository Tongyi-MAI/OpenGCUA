"""Controlled Win32 target for opt-in Windows adapter tests; no user app is touched."""

import ctypes
from ctypes import wintypes
import json
import os
import sys
import threading


def main():
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HINSTANCE
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.DefWindowProcW.restype = ctypes.c_ssize_t
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.DestroyWindow.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    callback = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    state = {"clicks": 0, "edit": None, "status": None}

    @callback
    def procedure(hwnd, message, wparam, lparam):
        if message == 0x0111 and (wparam & 0xFFFF) == 102:
            state["clicks"] += 1
            user.SetWindowTextW(state["status"], f"Clicks: {state['clicks']}")
            return 0
        if message == 0x0010:
            user.DestroyWindow(hwnd)
            return 0
        if message == 0x0002:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    class WindowClass(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("procedure", callback),
            ("class_extra", ctypes.c_int),
            ("window_extra", ctypes.c_int),
            ("instance", wintypes.HINSTANCE),
            ("icon", wintypes.HICON),
            ("cursor", wintypes.HANDLE),
            ("background", wintypes.HBRUSH),
            ("menu", wintypes.LPCWSTR),
            ("name", wintypes.LPCWSTR),
        ]

    instance = kernel.GetModuleHandleW(None)
    name = f"OpenGCUATest{os.getpid()}"
    registration = WindowClass(0, procedure, 0, 0, instance, None, None, 6, None, name)
    user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
    if not user.RegisterClassW(ctypes.byref(registration)):
        raise ctypes.WinError(ctypes.get_last_error())
    # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW; show without activating the target.
    window = user.CreateWindowExW(
        0x08000080,
        name,
        "OpenGCUA controlled test",
        0x00CF0000,
        40,
        40,
        460,
        220,
        None,
        None,
        instance,
        None,
    )
    if not window:
        raise ctypes.WinError(ctypes.get_last_error())
    state["edit"] = user.CreateWindowExW(
        0x200,
        "EDIT",
        "Initial fixture text",
        0x50010080,
        20,
        20,
        380,
        28,
        window,
        101,
        instance,
        None,
    )
    user.CreateWindowExW(
        0,
        "BUTTON",
        "Increment fixture counter",
        0x50010000,
        20,
        65,
        240,
        30,
        window,
        102,
        instance,
        None,
    )
    state["status"] = user.CreateWindowExW(
        0, "STATIC", "Clicks: 0", 0x50000000, 20, 110, 240, 25, window, 103, instance, None
    )
    user.ShowWindow(window, 4)  # SW_SHOWNOACTIVATE
    print(
        json.dumps(dict(pid=os.getpid(), hwnd=int(window), title="OpenGCUA controlled test")),
        flush=True,
    )

    def commands():
        for line in sys.stdin:
            if line.strip() == "state":
                text = ctypes.create_unicode_buffer(4096)
                user.GetWindowTextW(state["edit"], text, len(text))
                print(json.dumps(dict(text=text.value, clicks=state["clicks"])), flush=True)
            elif line.strip() == "close":
                user.PostMessageW(window, 0x0010, 0, 0)
                return
        user.PostMessageW(window, 0x0010, 0, 0)

    threading.Thread(target=commands, daemon=True).start()
    message = wintypes.MSG()
    while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user.TranslateMessage(ctypes.byref(message))
        user.DispatchMessageW(ctypes.byref(message))


if __name__ == "__main__":
    main()
