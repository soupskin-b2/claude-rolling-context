#!/usr/bin/env python3
"""A stand-in for the desktop app's main window (tests/test_switch_account.py):
a hidden Chrome_WidgetWin_1 window that, like the app, quits on WM_ENDSESSION
and ignores WM_CLOSE (the app hides to the tray). It writes what it got to
argv[1] as it goes.

  python tests/endsession_window.py OUT
"""
import ctypes
import sys
from ctypes import wintypes

u, k = ctypes.windll.user32, ctypes.windll.kernel32
OUT = sys.argv[1]
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.DefWindowProcW.restype = ctypes.c_ssize_t


def note(text):
    with open(OUT, "a") as f:
        f.write(text + "\n")


@WNDPROC
def proc(hwnd, msg, wparam, lparam):
    if msg == 0x11:                      # WM_QUERYENDSESSION
        note(f"query {lparam}")
        return 1
    if msg == 0x16:                      # WM_ENDSESSION
        note(f"end {wparam} {lparam}")
        if wparam:
            u.PostQuitMessage(0)
        return 0
    if msg == 0x10:                      # WM_CLOSE: hidden, not quit
        note("close")
        return 0
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


k.GetModuleHandleW.restype = wintypes.HINSTANCE
wc = WNDCLASS(lpfnWndProc=proc, hInstance=k.GetModuleHandleW(None), lpszClassName="Chrome_WidgetWin_1")
u.RegisterClassW(ctypes.byref(wc))
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                              wintypes.HINSTANCE, wintypes.LPVOID]
hwnd = u.CreateWindowExW(0, "Chrome_WidgetWin_1", "Claude", 0, 0, 0, 10, 10, None, None, wc.hInstance, None)
note("ready" if hwnd else "no window")
msg = wintypes.MSG()
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(msg))
    u.DispatchMessageW(ctypes.byref(msg))
note("quit")
