# -*- coding: utf-8 -*-
"""
SeeThroughWindow —— 把任意窗口变成半透明、置顶的「参考浮层」，默认仍然可以正常操作。

场景：跟着教程视频操作自己的软件、对着设计稿调界面、开会时把议程钉在角落。

纯 Python 标准库实现（ctypes 直调 Win32），无需 pip install 任何东西。
Windows 10 / 11，Python 3.8+。

用法
----
    python see_through_window.py                  # 启动
    pythonw see_through_window.py                 # 同上，但不弹控制台窗口
    python see_through_window.py --check-hotkeys  # 检查当前热键是否被别的程序占用
    python see_through_window.py --selftest       # 自检，只在末尾闪一下提示条

快捷键（全局，在其他程序里也生效；可在 config.ini 里改）
--------------------------------------------------------
    Ctrl+Alt+G        参考窗模式：置顶 + 70% 不透明，鼠标仍可正常操作（再按一次还原）
    Ctrl+Alt+T        只切换置顶
    Ctrl+Shift+Alt+X  点击穿透：浮层不再响应鼠标，点击落到下面的窗口（可选，默认关）
    Ctrl+Alt+[        更透明（不透明度 -10%）
    Ctrl+Alt+]        更不透明（不透明度 +10%）
    Ctrl+Alt+0        还原当前窗口；前台窗口没被改过时还原所有窗口
    Ctrl+Shift+Alt+Q  退出

关于「不透明度」：提示条里的数字是 opacity 不是透明度。
100% = 完全不透明，70% = 背后能透出三成，数字越小越透明。

关于点击穿透：它默认是关的。透明浮层依然是可交互的，你能正常点它、滚动它。
只有你主动按 Ctrl+Shift+Alt+X 才会开启穿透；开启后鼠标会完全穿过该窗口，
所以再关掉它也必须靠热键（热键是全局的，不看焦点）。

默认同一时刻只允许一个窗口处于穿透状态（config.ini 的 max-through 可改）。
只要有一个在穿透，无论前台是哪个窗口，穿透热键控制的都是那一个，
不会误开第二个——穿透窗口点不到，同时存在多个会很难收拾。
"""

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import traceback

# ============================================================== Win32 类型

c_void_p = ctypes.c_void_p
UINT = ctypes.c_uint
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

HWND = c_void_p
HDC = c_void_p
HMENU = c_void_p
HICON = c_void_p
HINSTANCE = c_void_p
HMODULE = c_void_p
HGDIOBJ = c_void_p
HRGN = c_void_p
HANDLE = c_void_p
HBRUSH = c_void_p
LPCWSTR = ctypes.c_wchar_p

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", UINT),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt", POINT),
        ("lPrivate", DWORD),
    ]


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", LPCWSTR),
        ("lpszClassName", LPCWSTR),
        ("hIconSm", HICON),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", HDC),
        ("fErase", BOOL),
        ("rcPaint", RECT),
        ("fRestore", BOOL),
        ("fIncUpdate", BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("hWnd", HWND),
        ("uID", UINT),
        ("uFlags", UINT),
        ("uCallbackMessage", UINT),
        ("hIcon", HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", DWORD),
        ("dwStateMask", DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", HICON),
    ]


# ============================================================== 函数签名
# 64 位下必须显式声明 restype，否则指针会被截断成 32 位——这是 ctypes 最经典的坑。


def _decl(lib, name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


# --- user32 ---
RegisterClassExW = _decl(user32, "RegisterClassExW", ctypes.c_uint16, ctypes.POINTER(WNDCLASSEX))
CreateWindowExW = _decl(
    user32, "CreateWindowExW", HWND,
    DWORD, LPCWSTR, LPCWSTR, DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, HMENU, HINSTANCE, ctypes.c_void_p,
)
DefWindowProcW = _decl(user32, "DefWindowProcW", LRESULT, HWND, UINT, WPARAM, LPARAM)
DestroyWindow = _decl(user32, "DestroyWindow", BOOL, HWND)
GetMessageW = _decl(user32, "GetMessageW", ctypes.c_int, ctypes.POINTER(MSG), HWND, UINT, UINT)
TranslateMessage = _decl(user32, "TranslateMessage", BOOL, ctypes.POINTER(MSG))
DispatchMessageW = _decl(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(MSG))
PostQuitMessage = _decl(user32, "PostQuitMessage", None, ctypes.c_int)
PostMessageW = _decl(user32, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM)
RegisterHotKey = _decl(user32, "RegisterHotKey", BOOL, HWND, ctypes.c_int, UINT, UINT)
UnregisterHotKey = _decl(user32, "UnregisterHotKey", BOOL, HWND, ctypes.c_int)
GetForegroundWindow = _decl(user32, "GetForegroundWindow", HWND)
GetShellWindow = _decl(user32, "GetShellWindow", HWND)
IsWindow = _decl(user32, "IsWindow", BOOL, HWND)
IsWindowVisible = _decl(user32, "IsWindowVisible", BOOL, HWND)
SetWindowPos = _decl(user32, "SetWindowPos", BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int,
                     ctypes.c_int, ctypes.c_int, UINT)
SetLayeredWindowAttributes = _decl(user32, "SetLayeredWindowAttributes", BOOL, HWND, DWORD,
                                   ctypes.c_byte, DWORD)
ShowWindow = _decl(user32, "ShowWindow", BOOL, HWND, ctypes.c_int)
SetTimer = _decl(user32, "SetTimer", ctypes.c_size_t, HWND, ctypes.c_size_t, UINT, ctypes.c_void_p)
KillTimer = _decl(user32, "KillTimer", BOOL, HWND, ctypes.c_size_t)
SetWindowTextW = _decl(user32, "SetWindowTextW", BOOL, HWND, LPCWSTR)
GetWindowTextW = _decl(user32, "GetWindowTextW", ctypes.c_int, HWND, ctypes.c_wchar_p,
                       ctypes.c_int)
GetWindowTextLengthW = _decl(user32, "GetWindowTextLengthW", ctypes.c_int, HWND)
GetClassNameW = _decl(user32, "GetClassNameW", ctypes.c_int, HWND, ctypes.c_wchar_p,
                      ctypes.c_int)
InvalidateRect = _decl(user32, "InvalidateRect", BOOL, HWND, ctypes.POINTER(RECT), BOOL)
UpdateWindow = _decl(user32, "UpdateWindow", BOOL, HWND)
BeginPaint = _decl(user32, "BeginPaint", HDC, HWND, ctypes.POINTER(PAINTSTRUCT))
EndPaint = _decl(user32, "EndPaint", BOOL, HWND, ctypes.POINTER(PAINTSTRUCT))
GetClientRect = _decl(user32, "GetClientRect", BOOL, HWND, ctypes.POINTER(RECT))
FillRect = _decl(user32, "FillRect", ctypes.c_int, HDC, ctypes.POINTER(RECT), HBRUSH)
DrawTextW = _decl(user32, "DrawTextW", ctypes.c_int, HDC, LPCWSTR, ctypes.c_int,
                  ctypes.POINTER(RECT), UINT)
GetDC = _decl(user32, "GetDC", HDC, HWND)
ReleaseDC = _decl(user32, "ReleaseDC", ctypes.c_int, HWND, HDC)
CreatePopupMenu = _decl(user32, "CreatePopupMenu", HMENU)
AppendMenuW = _decl(user32, "AppendMenuW", BOOL, HMENU, UINT, ctypes.c_size_t, LPCWSTR)
DestroyMenu = _decl(user32, "DestroyMenu", BOOL, HMENU)
TrackPopupMenu = _decl(user32, "TrackPopupMenu", ctypes.c_int, HMENU, UINT,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, ctypes.c_void_p)
SetForegroundWindow = _decl(user32, "SetForegroundWindow", BOOL, HWND)
GetCursorPos = _decl(user32, "GetCursorPos", BOOL, ctypes.POINTER(POINT))
LoadIconW = _decl(user32, "LoadIconW", HICON, HINSTANCE, ctypes.c_void_p)
LoadCursorW = _decl(user32, "LoadCursorW", ctypes.c_void_p, HINSTANCE, ctypes.c_void_p)
MessageBoxW = _decl(user32, "MessageBoxW", ctypes.c_int, HWND, LPCWSTR, LPCWSTR, UINT)
SystemParametersInfoW = _decl(user32, "SystemParametersInfoW", BOOL, UINT, UINT,
                              ctypes.c_void_p, UINT)
SetWindowRgn = _decl(user32, "SetWindowRgn", ctypes.c_int, HWND, HRGN, BOOL)
SetProcessDPIAware = _decl(user32, "SetProcessDPIAware", BOOL)

if hasattr(user32, "SetWindowLongPtrW"):
    _SetWindowLong = _decl(user32, "SetWindowLongPtrW", ctypes.c_ssize_t, HWND,
                           ctypes.c_int, ctypes.c_ssize_t)
    _GetWindowLong = _decl(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, HWND, ctypes.c_int)
else:  # 32 位 Python
    _SetWindowLong = _decl(user32, "SetWindowLongW", ctypes.c_long, HWND,
                           ctypes.c_int, ctypes.c_long)
    _GetWindowLong = _decl(user32, "GetWindowLongW", ctypes.c_long, HWND, ctypes.c_int)

# --- gdi32 ---
CreateSolidBrush = _decl(gdi32, "CreateSolidBrush", HBRUSH, DWORD)
DeleteObject = _decl(gdi32, "DeleteObject", BOOL, HGDIOBJ)
CreateRoundRectRgn = _decl(gdi32, "CreateRoundRectRgn", HRGN, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)
GetStockObject = _decl(gdi32, "GetStockObject", HGDIOBJ, ctypes.c_int)
SelectObject = _decl(gdi32, "SelectObject", HGDIOBJ, HDC, HGDIOBJ)
SetBkMode = _decl(gdi32, "SetBkMode", ctypes.c_int, HDC, ctypes.c_int)
SetTextColor = _decl(gdi32, "SetTextColor", DWORD, HDC, DWORD)
CreateCompatibleDC = _decl(gdi32, "CreateCompatibleDC", HDC, HDC)
CreateCompatibleBitmap = _decl(gdi32, "CreateCompatibleBitmap", HGDIOBJ, HDC,
                               ctypes.c_int, ctypes.c_int)
DeleteDC = _decl(gdi32, "DeleteDC", BOOL, HDC)

# --- shell32 ---
Shell_NotifyIconW = _decl(shell32, "Shell_NotifyIconW", BOOL, DWORD, ctypes.POINTER(NOTIFYICONDATA))

# --- kernel32 ---
GetModuleHandleW = _decl(kernel32, "GetModuleHandleW", HMODULE, LPCWSTR)
CreateMutexW = _decl(kernel32, "CreateMutexW", HANDLE, ctypes.c_void_p, BOOL, LPCWSTR)
GetConsoleWindow = _decl(kernel32, "GetConsoleWindow", HWND)

PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(BOOL, DWORD)
SetConsoleCtrlHandler = _decl(kernel32, "SetConsoleCtrlHandler", BOOL,
                              PHANDLER_ROUTINE, BOOL)
GetConsoleProcessList = _decl(kernel32, "GetConsoleProcessList", DWORD,
                              ctypes.POINTER(DWORD), DWORD)

# ============================================================== 常量

GWL_EXSTYLE = -20

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

WS_VISIBLE = 0x10000000

LWA_ALPHA = 0x00000002

HWND_TOPMOST = ctypes.c_void_p(-1)
HWND_NOTOPMOST = ctypes.c_void_p(-2)

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_ERASEBKGND = 0x0014
WM_NULL = 0x0000
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0040

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

DT_LEFT = 0x0000
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_WORDBREAK = 0x0010
DT_CALCRECT = 0x0400
DT_NOPREFIX = 0x0800

SPI_GETWORKAREA = 0x0030
TRANSPARENT = 1
DEFAULT_GUI_FONT = 17
IDI_APPLICATION = 32512
IDC_ARROW = 32512

ERROR_ALREADY_EXISTS = 183

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
_CONSOLE_EVENTS = (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT,
                   CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT)


def rgb(r, g, b):
    """COLORREF 是 0x00BBGGRR，不是 RGB。"""
    return (b << 16) | (g << 8) | r


def get_ex_style(hwnd):
    return _GetWindowLong(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF


def set_ex_style(hwnd, value):
    return _SetWindowLong(hwnd, GWL_EXSTYLE, value)


# ============================================================== 窗口状态


class WindowState(object):
    """记住一个窗口被我们改动之前的原始样式，才能干净地还原。"""

    __slots__ = ("hwnd", "original_ex", "original_topmost", "on_top", "through", "alpha",
                 "title", "cls")

    def __init__(self, hwnd, original_ex, title="", cls=""):
        self.hwnd = hwnd
        self.original_ex = original_ex
        self.original_topmost = bool(original_ex & WS_EX_TOPMOST)
        self.on_top = False
        self.through = False
        self.alpha = 100
        self.title = title
        self.cls = cls

    @property
    def ghost(self):
        """透明浮层是否开启。点击穿透不算在内——它是独立的、默认关闭的开关。"""
        return self.on_top and self.alpha < 100


# ---------------------------------------------------------- 崩溃后的自愈
# 进程被强杀时（关终端标签、任务管理器结束进程）根本没有机会执行还原代码。
# 所以每次改动窗口样式都把「原始样式」落盘，下次启动时先把上次的残留修好。


STATE_FILE_NAME = "see-through-window-state.json"


def state_file_path():
    return os.path.join(config_dir(), STATE_FILE_NAME)


def window_caption(hwnd):
    try:
        length = GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 2)
        GetWindowTextW(hwnd, buffer, length + 2)
        return buffer.value
    except Exception:
        return ""


def window_class(hwnd):
    try:
        buffer = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, buffer, 256)
        return buffer.value
    except Exception:
        return ""


def write_state_file(entries):
    """把「窗口 → 原始样式」写到磁盘，供下次启动时修复。"""
    path = state_file_path()
    try:
        if not entries:
            clear_state_file()
            return
        payload = {"version": 1, "windows": entries}
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        if os.path.exists(path):
            os.remove(path)
        os.rename(temporary, path)
    except Exception:
        pass


def read_state_file():
    try:
        with open(state_file_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return []
        windows = payload.get("windows")
        return windows if isinstance(windows, list) else []
    except Exception:
        return []


def clear_state_file():
    try:
        path = state_file_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def apply_state(state):
    """把状态写进真实的窗口样式里。"""
    hwnd = state.hwnd
    if not hwnd or not IsWindow(hwnd):
        return False

    want = state.original_ex
    layered = state.alpha < 100
    if layered:
        want |= WS_EX_LAYERED
    else:
        want &= ~WS_EX_LAYERED
    if state.through:
        want |= WS_EX_TRANSPARENT
    else:
        want &= ~WS_EX_TRANSPARENT
    want &= 0xFFFFFFFF

    current = get_ex_style(hwnd)
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    if current != want:
        # SetWindowLongPtr 失败时返回 0；0 也可能是合法的旧值，
        # 所以必须先把 last error 清零再看，不然会把失败当成成功。
        ctypes.set_last_error(0)
        previous = set_ex_style(hwnd, want)
        if previous == 0 and ctypes.get_last_error() != 0:
            return False
        flags |= SWP_FRAMECHANGED

    if layered:
        alpha = max(1, min(255, int(round(state.alpha * 255.0 / 100.0))))
        if not SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA):
            return False

    top = state.on_top or state.original_topmost
    if not SetWindowPos(hwnd, HWND_TOPMOST if top else HWND_NOTOPMOST, 0, 0, 0, 0, flags):
        return False
    return True


def reset_state(state):
    """精确还原到改动前的样子。"""
    hwnd = state.hwnd
    if not hwnd or not IsWindow(hwnd):
        return False
    set_ex_style(hwnd, state.original_ex)
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED
    SetWindowPos(hwnd, HWND_TOPMOST if state.original_topmost else HWND_NOTOPMOST,
                 0, 0, 0, 0, flags)
    return True


# ============================================================== 消息分发

HANDLERS = {}


def _window_proc(hwnd, msg, wparam, lparam):
    key = int(hwnd) if hwnd else 0
    handler = HANDLERS.get(key)
    if handler is not None:
        result = handler(hwnd, msg, wparam, lparam)
        if result is not None:
            return result
    return DefWindowProcW(hwnd, msg, wparam, lparam)


# 必须持有引用，否则回调被 GC 掉后程序直接崩
_WNDPROC_REF = WNDPROC(_window_proc)
_CLASS_NAMES = []
_HINSTANCE = None


def ensure_class(class_name, hbr_background=None, hicon=None):
    """注册一个窗口类；已注册则跳过。"""
    global _HINSTANCE
    if _HINSTANCE is None:
        _HINSTANCE = GetModuleHandleW(None)

    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.style = 0
    wc.lpfnWndProc = _WNDPROC_REF
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = _HINSTANCE
    wc.hIcon = hicon or LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
    wc.hCursor = LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
    wc.hbrBackground = hbr_background or 0
    wc.lpszMenuName = None
    wc.lpszClassName = class_name
    wc.hIconSm = wc.hIcon

    atom = RegisterClassExW(ctypes.byref(wc))
    if not atom:
        err = ctypes.get_last_error()
        # 1410 = 类已存在，重复运行同一个程序时可能出现
        if err != 1410:
            raise ctypes.WinError(err)
    _CLASS_NAMES.append(class_name)
    return atom


def create_window(class_name, title, ex_style, style, x, y, w, h, parent=None):
    hwnd = CreateWindowExW(
        ex_style, class_name, title, style,
        x, y, w, h, parent, None, _HINSTANCE, None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


# ============================================================== 提示条（Toast）


class Toast(object):
    """一个不抢焦点、几秒后自动消失的小提示窗。"""

    CLASS_NAME = "SeeThroughWindowToast"
    PAD_X = 18
    PAD_Y = 10
    MAX_WIDTH = 520
    TIMER_ID = 1
    BOTTOM_MARGIN = 56

    def __init__(self):
        self.hwnd = None
        self.text = ""
        self._timer = 0
        ensure_class(self.CLASS_NAME)
        self.hwnd = create_window(
            self.CLASS_NAME, "SeeThroughWindow", WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            0x80000000, 0, 0, 10, 10,  # WS_POPUP
        )
        HANDLERS[int(self.hwnd)] = self._wnd_proc

    # -- 测量与定位 ------------------------------------------------------
    def _measure(self, text):
        hdc = GetDC(None)
        old = SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT))
        try:
            rc = RECT(0, 0, self.MAX_WIDTH, 1 << 20)
            DrawTextW(hdc, text, -1, ctypes.byref(rc),
                      DT_WORDBREAK | DT_CALCRECT | DT_NOPREFIX)
            width = min(self.MAX_WIDTH, rc.right) + self.PAD_X * 2
            height = max(24, rc.bottom) + self.PAD_Y * 2
        finally:
            SelectObject(hdc, old)
            ReleaseDC(None, hdc)
        return width, height

    def show(self, text, duration=1800):
        self.text = text
        width, height = self._measure(text)

        wa = RECT()
        SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0)
        x = wa.left + (wa.right - wa.left - width) // 2
        y = wa.bottom - height - self.BOTTOM_MARGIN

        SetWindowTextW(self.hwnd, text)
        SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, width, height,
                     SWP_NOACTIVATE | SWP_SHOWWINDOW)
        rgn = CreateRoundRectRgn(0, 0, width + 1, height + 1, 16, 16)
        SetWindowRgn(self.hwnd, rgn, True)
        InvalidateRect(self.hwnd, None, True)
        UpdateWindow(self.hwnd)
        ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

        if self._timer:
            KillTimer(self.hwnd, self.TIMER_ID)
        self._timer = SetTimer(self.hwnd, self.TIMER_ID, duration, None)

    def hide(self):
        if self._timer:
            KillTimer(self.hwnd, self.TIMER_ID)
            self._timer = 0
        ShowWindow(self.hwnd, SW_HIDE)

    # -- 绘制 ------------------------------------------------------------
    def _paint(self, hdc, width, height):
        """把提示条画到任意 HDC 上；抽出来是为了能用内存 DC 做无窗口自检。"""
        brush = CreateSolidBrush(rgb(24, 24, 28))
        try:
            rc = RECT(0, 0, width, height)
            FillRect(hdc, ctypes.byref(rc), brush)
        finally:
            DeleteObject(brush)

        SetBkMode(hdc, TRANSPARENT)
        SetTextColor(hdc, rgb(240, 240, 245))
        old = SelectObject(hdc, GetStockObject(DEFAULT_GUI_FONT))
        try:
            inner = RECT(self.PAD_X, self.PAD_Y, width - self.PAD_X, height - self.PAD_Y)
            DrawTextW(hdc, self.text, -1, ctypes.byref(inner),
                      DT_CENTER | DT_VCENTER | DT_WORDBREAK | DT_NOPREFIX)
        finally:
            SelectObject(hdc, old)

    # -- 窗口过程 --------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = BeginPaint(hwnd, ctypes.byref(ps))
            try:
                rc = RECT()
                GetClientRect(hwnd, ctypes.byref(rc))
                self._paint(hdc, rc.right, rc.bottom)
            finally:
                EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_TIMER:
            self.hide()
            return 0
        return None


# ============================================================== 主程序


class Hotkey(object):
    __slots__ = ("ident", "mods", "vk", "label", "pretty")

    def __init__(self, ident, mods, vk, label, pretty):
        self.ident = ident
        self.mods = mods
        self.vk = vk
        self.label = label
        self.pretty = pretty


# 默认热键写在代码里，用户可以改同目录的 config.ini 覆盖。
# 说明：Windows 的全局热键是「先到先得」，QQ/微信/截图工具常占用 ctrl+alt+字母，
# 所以独立的开关（比如点击穿透）默认用三个修饰键，撞车概率低得多。
HOTKEY_DEFAULTS = [
    ("ghost",      "参考窗模式（置顶 + 透明，可交互）", "ctrl+alt+g"),
    ("top",        "只切换置顶",                        "ctrl+alt+t"),
    ("through",    "点击穿透（可选，默认关）",            "ctrl+shift+alt+x"),
    ("alpha-down", "更透明",                            "ctrl+alt+["),
    ("alpha-up",   "更不透明",                          "ctrl+alt+]"),
    ("reset",      "还原当前窗口",                       "ctrl+alt+0"),
    ("quit",       "退出",                              "ctrl+shift+alt+q"),
]

MOD_WIN = 0x0008

VK_NAMES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "\\": 0xDC, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "`": 0xC0,
}


def vk_for(name):
    """把 config.ini 里写的键名转成虚拟键码。"""
    name = name.strip().lower()
    if len(name) == 1:
        char = name.upper()
        if ("A" <= char <= "Z") or ("0" <= char <= "9"):
            return ord(char)
    if name in VK_NAMES:
        return VK_NAMES[name]
    if name.startswith("f") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    raise ValueError("不认识的键：%s" % name)


def parse_hotkey(spec):
    mods = 0
    vk = None
    for part in [p.strip().lower() for p in spec.split("+")]:
        if not part:
            continue
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "meta"):
            mods |= MOD_WIN
        else:
            vk = vk_for(part)
    if vk is None:
        raise ValueError("缺少主键：%s" % spec)
    return mods, vk


def pretty_hotkey(spec):
    parts = []
    for part in [p.strip() for p in spec.split("+") if p.strip()]:
        low = part.lower()
        if low in ("ctrl", "control"):
            parts.append("Ctrl")
        elif low == "alt":
            parts.append("Alt")
        elif low == "shift":
            parts.append("Shift")
        elif low in ("win", "meta"):
            parts.append("Win")
        elif len(part) == 1:
            parts.append(part.upper())
        else:
            parts.append(part.capitalize())
    return "+".join(parts)


def config_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


def config_path():
    return os.path.join(config_dir(), "config.ini")


DEFAULT_MAX_THROUGH = 1


def load_config():
    """读 config.ini；不存在就先按默认值生成一份，方便用户直接改。"""
    path = config_path()

    if not os.path.exists(path):
        try:
            lines = [
                "# SeeThroughWindow 配置（改完重启生效）",
                "",
                "# ----------------------------------------------------------",
                "# 点击穿透：同一时刻最多允许几个窗口处于穿透状态",
                "#   1 = 只允许一个（默认）。此时无论前台是哪个窗口，",
                "#       按穿透热键都是在开/关那唯一的一个，不会误开第二个。",
                "#   2 及以上 = 允许这么多个，达到上限后需先关掉一个。",
                "#   0 = 不限制（不建议，穿透窗口点不到，多了会难关）。",
                "# ----------------------------------------------------------",
                "max-through = %d" % DEFAULT_MAX_THROUGH,
                "",
                "# ----------------------------------------------------------",
                "# 热键",
                "#   写法：ctrl+alt+g",
                "#   修饰键：ctrl / alt / shift / win，可任意组合",
                "#   主键：a-z、0-9、f1-f24、[ ] ; ' , . / \\ - = ` 以及 space/tab/enter/esc",
                "#",
                "# 注意：Windows 的全局热键先到先得。如果某个组合已经被别的程序",
                "#（QQ、微信、截图工具等）占用，这里写的就没法生效，",
                "# 启动时会在终端里标出 [占用]，换一个组合即可。",
                "# ----------------------------------------------------------",
                "",
            ]
            for key, label, default in HOTKEY_DEFAULTS:
                lines.append("# %s" % label)
                lines.append("%s = %s" % (key, default))
                lines.append("")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
        except Exception:
            pass

    specs = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                specs[key.strip().lower()] = value.strip()
    except Exception:
        pass
    return specs


def parse_max_through(specs):
    """读「同时允许多少个窗口穿透」，写错了就回退默认值。"""
    raw = specs.get("max-through")
    if raw is None or raw == "":
        return DEFAULT_MAX_THROUGH, ""
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_THROUGH, "max-through = %s 不是整数，已回退 %d" % (
            raw, DEFAULT_MAX_THROUGH)
    if value < 0:
        return DEFAULT_MAX_THROUGH, "max-through 不能为负，已回退 %d" % DEFAULT_MAX_THROUGH
    return value, ""


def build_hotkeys():
    """返回 (热键列表, 配置问题列表, 设置字典)。写错的项会回退到默认值。"""
    specs = load_config()
    hotkeys = []
    problems = []

    for ident, (key, label, default) in enumerate(HOTKEY_DEFAULTS, start=1):
        spec = specs.get(key) or default
        try:
            mods, vk = parse_hotkey(spec)
        except Exception as exc:
            problems.append("%s = %s（%s），已回退默认值" % (key, spec, exc))
            mods, vk = parse_hotkey(default)
            spec = default
        hotkeys.append(Hotkey(ident, mods, vk, label, pretty_hotkey(spec)))

    max_through, problem = parse_max_through(specs)
    if problem:
        problems.append(problem)

    return hotkeys, problems, {"max_through": max_through}


GHOST_ALPHA = 70
ALPHA_STEP = 10
ALPHA_MIN = 20
QUIT_HOTKEY_ID = 7


class SeeThroughWindow(object):
    CLASS_NAME = "SeeThroughWindowHost"
    TRAY_UID = 1
    MENU_RESET_ALL = 1001
    MENU_HELP = 1002
    MENU_QUIT = 1003

    def __init__(self):
        self.hwnd = None
        self.toast = None
        self.states = {}
        self.hotkeys = []
        self.hotkey_problems = []
        self.settings = {"max_through": DEFAULT_MAX_THROUGH}
        self.max_through = DEFAULT_MAX_THROUGH
        self.occupied = []
        self.registered_hotkeys = []
        self.tray_added = False
        self.tray_ok = False
        self.icon = None
        self.quitting = False

    # -- 启动 ------------------------------------------------------------
    def start(self):
        self.hotkeys, self.hotkey_problems, self.settings = build_hotkeys()
        self.max_through = self.settings["max_through"]

        self.icon = LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        ensure_class(self.CLASS_NAME, hicon=self.icon)
        self.hwnd = create_window(
            self.CLASS_NAME, "SeeThroughWindow", WS_EX_TOOLWINDOW, 0, 0, 0, 0, 0,
        )
        HANDLERS[int(self.hwnd)] = self._wnd_proc

        self.toast = Toast()
        atexit.register(self._restore_on_exit)

        # 上一轮如果被强杀（关终端、任务管理器结束进程），还原代码根本来不及跑。
        # 这里把上次留下的残留先修掉，再说别的。
        recovered = self._recover_leftovers()

        self._register_hotkeys()
        self._add_tray()
        self._report_hotkeys()

        quit_ready = QUIT_HOTKEY_ID in self.registered_hotkeys
        if not self.tray_ok and not quit_ready:
            MessageBoxW(
                None,
                "托盘不可用，退出热键也被占用——程序将无法被控制，即将退出。\n\n"
                "把 config.ini 里的 quit 换成别的组合，或改用 python.org 版本的 Python。",
                "SeeThroughWindow", 0x00000010,
            )
            self.quit()
            return

        if not self.tray_ok:
            message = ("托盘不可用（微软商店版 Python 不支持经典托盘）\n"
                       "退出：%s" % self._pretty_of(QUIT_HOTKEY_ID))
            if recovered:
                message += "\n已自动恢复上次没还原的 %d 个窗口" % recovered
            self.toast.show(message, 6000)
            return

        if self.occupied:
            names = "、".join(hk.pretty for hk in self.occupied[:2])
            self.toast.show(
                "%s 已被其他程序占用（多半是 QQ/微信）\n"
                "改 config.ini 换一组，重启生效" % names, 6000,
            )
            return

        if self.hotkey_problems:
            self.toast.show("config.ini 有写法错误，已回退默认值", 5000)
            return

        if recovered:
            self.toast.show("已自动恢复上次没还原的 %d 个窗口" % recovered, 3600)
            return

        self.toast.show(
            "已启动 · %s 参考窗模式" % self._pretty_of(1), 2600,
        )

    def _pretty_of(self, ident):
        for hotkey in self.hotkeys:
            if hotkey.ident == ident:
                return hotkey.pretty
        return ""

    def _report_hotkeys(self):
        """把注册结果打到终端——用户能看到到底哪个键没抢到。"""
        if sys.stdout is None:
            return
        lines = ["", "热键注册状态："]
        for hotkey in self.hotkeys:
            mark = "[OK]  " if hotkey.ident in self.registered_hotkeys else "[占用]"
            lines.append("  %s %-18s %s" % (mark, hotkey.pretty, hotkey.label))
        for problem in self.hotkey_problems:
            lines.append("  [配置] " + problem)
        if self.occupied:
            lines.append("")
            lines.append("被占用的组合通常是别的程序先注册了（QQ / 微信 / 截图工具）。")
            lines.append("改同目录的 %s 换一组即可，重启生效。" % config_path())
        lines.append("")
        lines.append("点击穿透上限：%s" % (
            "%d 个窗口（同一时刻只控制那一个）" % self.max_through
            if self.max_through == 1 else
            ("不限制" if self.max_through == 0 else "%d 个窗口" % self.max_through)))
        lines.append("")
        try:
            print("\n".join(lines))
        except Exception:
            pass

    def _register_hotkeys(self):
        self.registered_hotkeys = []
        self.occupied = []
        for hotkey in self.hotkeys:
            if RegisterHotKey(self.hwnd, hotkey.ident, hotkey.mods, hotkey.vk):
                self.registered_hotkeys.append(hotkey.ident)
            else:
                self.occupied.append(hotkey)

    def _unregister_hotkeys(self):
        for ident in self.registered_hotkeys:
            UnregisterHotKey(self.hwnd, ident)
        self.registered_hotkeys = []

    # -- 托盘 ------------------------------------------------------------
    def _notify_data(self, flags):
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = self.TRAY_UID
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self.icon
        nid.szTip = "SeeThroughWindow · 参考窗"
        return nid

    def _add_tray(self):
        nid = self._notify_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        self.tray_ok = bool(Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        self.tray_added = self.tray_ok
        return self.tray_ok

    def _remove_tray(self):
        if self.tray_added:
            nid = self._notify_data(0)
            Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self.tray_added = False

    def _show_menu(self):
        menu = CreatePopupMenu()
        if not menu:
            return
        try:
            AppendMenuW(menu, MF_STRING, self.MENU_RESET_ALL, "还原所有窗口")
            AppendMenuW(menu, MF_STRING, self.MENU_HELP, "快捷键说明")
            AppendMenuW(menu, MF_SEPARATOR, 0, None)
            AppendMenuW(menu, MF_STRING, self.MENU_QUIT, "退出")

            pt = POINT()
            GetCursorPos(ctypes.byref(pt))
            # 让菜单能被正常点掉
            SetForegroundWindow(self.hwnd)
            cmd = TrackPopupMenu(
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
                pt.x, pt.y, 0, self.hwnd, None,
            )
            PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            DestroyMenu(menu)

        if cmd == self.MENU_RESET_ALL:
            self.reset_all()
        elif cmd == self.MENU_HELP:
            self.toast.show(
                "\n".join("%s  %s" % (hk.pretty, hk.label) for hk in self.hotkeys), 6000,
            )
        elif cmd == self.MENU_QUIT:
            self.quit()

    # -- 目标窗口 --------------------------------------------------------
    def _is_own_window(self, hwnd):
        return int(hwnd) in (int(self.hwnd or 0), int(getattr(self.toast, "hwnd", 0) or 0))

    def _foreground_target(self):
        """当前前台窗口，而且必须是「能改、值得改」的那种。

        限制：桌面/任务栏（shell）、本程序自己的窗口、不可见窗口一律跳过。
        对它们改样式没有意义，还会让人以为工具坏了。
        """
        hwnd = GetForegroundWindow()
        if not hwnd:
            return 0
        if hwnd == GetShellWindow():
            return 0
        if self._is_own_window(hwnd):
            return 0
        if not IsWindow(hwnd):
            return 0
        if not IsWindowVisible(hwnd):
            return 0
        return hwnd

    def _through_states(self):
        """当前处于点击穿透状态、仍然存活的窗口。

        穿透窗口完全点不到，只能靠热键控制——所以每次按键都要先认一遍它们。
        """
        states = []
        for key in list(self.states.keys()):
            state = self.states[key]
            if not state.through:
                continue
            if not IsWindow(ctypes.c_void_p(key)):
                self.states.pop(key, None)
                continue
            states.append(state)
        return states

    def _state_for(self, hwnd):
        key = int(hwnd)
        state = self.states.get(key)
        if state is None:
            state = WindowState(hwnd, get_ex_style(hwnd),
                                title=window_caption(hwnd), cls=window_class(hwnd))
            self.states[key] = state
        return state

    def _persist(self):
        """把当前还处于「被我改过」状态的窗口写进磁盘。

        进程被强杀时来不及还原，下次启动靠这个文件把残留修好。
        """
        entries = []
        for key, state in self.states.items():
            if not IsWindow(ctypes.c_void_p(key)):
                continue
            entries.append({
                "hwnd": key,
                "ex": state.original_ex,
                "topmost": bool(state.original_topmost),
                "title": state.title,
                "class": state.cls,
            })
        write_state_file(entries)

    def _recover_leftovers(self):
        """启动时修复上次没还原干净的窗口。返回修好数量。"""
        entries = read_state_file()
        if not entries:
            return 0

        restored = 0
        for entry in entries:
            try:
                key = int(entry.get("hwnd", 0))
                if not key:
                    continue
                hwnd = ctypes.c_void_p(key)
                if not IsWindow(hwnd):
                    continue
                # 窗口编号会被系统复用。标题对不上就说明已经是别的窗口了，不能乱改。
                saved_title = entry.get("title") or ""
                if saved_title and window_caption(hwnd) != saved_title:
                    continue
                set_ex_style(hwnd, int(entry.get("ex", 0)))
                SetWindowPos(
                    hwnd,
                    HWND_TOPMOST if entry.get("topmost") else HWND_NOTOPMOST,
                    0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
                )
                restored += 1
            except Exception:
                continue
        clear_state_file()
        return restored

    def _restore_on_exit(self):
        """解释器正常退出时的最后一道保险（包括未捕获异常）。"""
        try:
            self.reset_all(quiet=True)
        except Exception:
            pass

    def _apply_or_revert(self, state, snapshot):
        """写样式；写不进去就回滚并说清楚原因（管理员窗口改不了）。"""
        if apply_state(state):
            self._persist()
            return True
        (state.on_top, state.through, state.alpha) = snapshot
        self.toast.show("这个窗口改不了样式\n可能是以管理员身份运行的窗口", 3200)
        return False

    # -- 动作 ------------------------------------------------------------
    def toggle_ghost(self):
        hwnd = self._foreground_target()
        if not hwnd:
            self.toast.show("没有可操作的前台窗口")
            return
        state = self._state_for(hwnd)
        snapshot = (state.on_top, state.through, state.alpha)

        if state.ghost:
            state.on_top = False
            state.through = False
            state.alpha = 100
            if self._apply_or_revert(state, snapshot):
                self.toast.show("参考窗：已还原")
            return

        # 默认进入可交互的透明浮层：置顶 + 半透明，但鼠标照常能用。
        # 点击穿透要另外按 Ctrl+Shift+Alt+X 才开启，避免"一按就点不动"。
        state.on_top = True
        state.through = False
        state.alpha = GHOST_ALPHA
        if self._apply_or_revert(state, snapshot):
            self.toast.show("参考窗：开（不透明度 %d%%，可交互）" % state.alpha)

    def toggle_top(self):
        hwnd = self._foreground_target()
        if not hwnd:
            self.toast.show("没有可操作的前台窗口")
            return
        state = self._state_for(hwnd)
        snapshot = (state.on_top, state.through, state.alpha)
        state.on_top = not state.on_top
        if self._apply_or_revert(state, snapshot):
            self.toast.show("置顶：开" if state.on_top else "置顶：关")

    def toggle_through(self):
        """开/关点击穿透。

        默认只允许一个窗口穿透（config.ini 里的 max-through = 1）：
        透明浮层还能用鼠标操作，穿透窗口却完全点不到——同时存在多个，
        一旦忘了哪个是哪个就再也关不掉了。所以只要有一个在穿透状态，
        无论当前前台是哪个窗口，热键控制的都是那一个。
        """
        foreground = self._foreground_target()
        living = self._through_states()

        # 前台窗口自己就在穿透状态 → 关掉它
        if foreground and int(foreground) in self.states and self.states[int(foreground)].through:
            self._set_through(self.states[int(foreground)], False)
            return

        # 默认策略：只允许一个。它在哪个窗口不重要，热键就是在控制它。
        if self.max_through == 1 and living:
            self._set_through(living[0], False)
            return

        if self.max_through > 0 and len(living) >= self.max_through:
            self.toast.show(
                "已有 %d 个窗口在穿透（上限 %d）\n先关掉一个再开新的"
                % (len(living), self.max_through), 3200)
            return

        if not foreground:
            self.toast.show("没有可操作的前台窗口")
            return

        self._set_through(self._state_for(foreground), True)

    def _set_through(self, state, value):
        snapshot = (state.on_top, state.through, state.alpha)
        state.through = value
        if not self._apply_or_revert(state, snapshot):
            return
        if value:
            self.toast.show("点击穿透：开\n窗口已点不到，再按一次关掉", 3200)
        else:
            self.toast.show("点击穿透：关")

    def change_alpha(self, delta):
        hwnd = self._foreground_target()
        if hwnd and int(hwnd) in self.states:
            state = self.states[int(hwnd)]
        else:
            stuck = self._through_states()
            if not stuck:
                self.toast.show("先按 %s 把窗口变成浮层" % self._pretty_of(1), 3200)
                return
            state = stuck[-1]

        snapshot = (state.on_top, state.through, state.alpha)
        state.alpha = max(ALPHA_MIN, min(100, state.alpha + delta))
        if self._apply_or_revert(state, snapshot):
            self.toast.show("不透明度：%d%%" % state.alpha)

    def reset_target(self):
        hwnd = self._foreground_target()
        if hwnd and int(hwnd) in self.states:
            state = self.states.pop(int(hwnd))
            reset_state(state)
            self._persist()
            self.toast.show("已还原")
            return

        # 兜底：前台窗口没被改过，就把所有改过的窗口都还回去。
        # 这样哪怕有窗口卡在穿透里，这个键也一定能救回来。
        count = self.reset_all(quiet=True)
        if count:
            self.toast.show("已还原 %d 个窗口" % count)
        else:
            self.toast.show("这个窗口没有被改动过")

    def reset_all(self, quiet=False):
        count = 0
        for key in list(self.states.keys()):
            state = self.states.pop(key)
            if reset_state(state):
                count += 1
        self._persist()
        if quiet:
            return count
        self.toast.show(("已还原 %d 个窗口" % count) if count else "没有需要还原的窗口")
        return count

    # -- 消息处理 --------------------------------------------------------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_HOTKEY:
            self._on_hotkey(int(wparam))
            return 0
        if msg == WM_TRAYICON:
            event = int(lparam) & 0xFFFF
            if event == WM_RBUTTONUP:
                self._show_menu()
            elif event == WM_LBUTTONDBLCLK:
                self.toast.show("Ctrl+Alt+G 参考窗模式\n右键托盘图标可还原或退出", 4000)
            return 0
        if msg == WM_DESTROY:
            PostQuitMessage(0)
            return 0
        return None

    def _on_hotkey(self, ident):
        if ident == 1:
            self.toggle_ghost()
        elif ident == 2:
            self.toggle_top()
        elif ident == 3:
            self.toggle_through()
        elif ident == 4:
            self.change_alpha(-ALPHA_STEP)
        elif ident == 5:
            self.change_alpha(+ALPHA_STEP)
        elif ident == 6:
            self.reset_target()
        elif ident == 7:
            self.quit()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self._unregister_hotkeys()
        self._remove_tray()
        PostQuitMessage(0)

    # -- 主循环 ----------------------------------------------------------
    def run(self):
        self.start()
        msg = MSG()
        while True:
            got = GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got <= 0:
                break
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))
        self.reset_all(quiet=True)
        self._unregister_hotkeys()
        self._remove_tray()
        for hwnd in list(HANDLERS.keys()):
            try:
                DestroyWindow(ctypes.c_void_p(hwnd))
            except Exception:
                pass
        HANDLERS.clear()


# ============================================================== 自检


def has_package_identity():
    """微软商店版 Python 带 MSIX 包标识；打包进程调用经典托盘 API 会被拒绝。"""
    try:
        fn = getattr(kernel32, "GetCurrentPackageFullName", None)
        if fn is None:
            return False
        fn.restype = ctypes.c_long
        fn.argtypes = [ctypes.POINTER(UINT), LPCWSTR]
        length = UINT(0)
        rc = fn(ctypes.byref(length), None)
        return rc != 15700  # 15700 = APPMODEL_ERROR_NO_PACKAGE
    except Exception:
        return False


def check_hotkeys():
    """逐个试注册配置里的热键，报告哪个被别的程序占了。

    不用启动整个工具就能知道该改 config.ini 的哪一行。
    """
    hotkeys, problems = build_hotkeys()[:2]
    ensure_class("SeeThroughWindowProbe")
    probe = create_window("SeeThroughWindowProbe", "probe", 0,
                          WS_VISIBLE, -3000, -3000, 16, 16)
    occupied = 0
    try:
        print("")
        print("热键可用性检查：%s" % config_path())
        print("")
        for hotkey in hotkeys:
            if RegisterHotKey(probe, hotkey.ident, hotkey.mods, hotkey.vk):
                UnregisterHotKey(probe, hotkey.ident)
                print("  [可用] %-20s %s" % (hotkey.pretty, hotkey.label))
            else:
                occupied += 1
                print("  [占用] %-20s %s  <- 被别的程序占用" % (hotkey.pretty, hotkey.label))
        for problem in problems:
            print("  [配置] " + problem)
        print("")
        if occupied:
            print("有 %d 个组合被占用。改 %s 里对应那几行，重启工具生效。"
                  % (occupied, config_path()))
        else:
            print("全部可用。")
    finally:
        DestroyWindow(probe)
    return 0


def selftest():
    """不显示任何窗口，验证 Win32 这一层是否真的工作。"""
    lines = []
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        if ok:
            lines.append("  PASS  %s %s" % (name, detail))
        else:
            failures += 1
            lines.append("  FAIL  %s %s" % (name, detail))

    try:
        SetProcessDPIAware()
        lines.append("python %s" % sys.version.split()[0])
        lines.append("sizeof(NOTIFYICONDATA)=%d" % ctypes.sizeof(NOTIFYICONDATA))
        lines.append("sizeof(MSG)=%d" % ctypes.sizeof(MSG))

        # 1. 注册窗口类 + 创建探针窗口
        # 注意：探针必须是「可见」窗口。隐藏窗口没有 z-order 层级，
        # SetWindowPos(HWND_TOPMOST) 会返回成功但不会真的置上 TOPMOST 位——
        # 用隐藏窗口做断言会得到假失败。这里把它放在屏幕外，不打扰用户。
        try:
            ensure_class("SeeThroughWindowSelfTest")
            probe = create_window("SeeThroughWindowSelfTest", "probe", 0,
                                  WS_VISIBLE, -3000, -3000, 40, 30)
            check("创建窗口", bool(probe))
        except Exception as exc:
            check("创建窗口", False, str(exc))
            probe = None

        # 2. 扩展样式环绕
        if probe:
            original = get_ex_style(probe)
            state = WindowState(probe, original)
            state.on_top = True
            state.through = True
            state.alpha = 60
            applied = apply_state(state)
            ex = get_ex_style(probe)
            check("apply_state 返回 True", applied)
            check("已置 LAYERED 位", bool(ex & WS_EX_LAYERED), hex(ex))
            check("已置 TRANSPARENT 位", bool(ex & WS_EX_TRANSPARENT), hex(ex))
            check("已置 TOPMOST 位", bool(ex & WS_EX_TOPMOST), hex(ex))

            ok = SetLayeredWindowAttributes(probe, 0, 153, LWA_ALPHA)
            check("SetLayeredWindowAttributes", bool(ok), "err=%s" % ctypes.get_last_error())

            reset_state(state)
            restored = get_ex_style(probe)
            check("reset 还原原始样式", restored == original,
                  "%s vs %s" % (hex(restored), hex(original)))

        # 3. 热键
        # 不能用固定组合断言：机器上任何程序都可能占着它（包括本工具的另一个实例）。
        # 依次试几个冷门组合，只要有一个能注册就说明这一层是通的。
        if probe:
            registered = None
            tried = []
            for spec in ("ctrl+shift+alt+f9", "ctrl+shift+f9", "ctrl+alt+f10"):
                mods, vk = parse_hotkey(spec)
                tried.append(spec)
                if RegisterHotKey(probe, 99, mods, vk):
                    registered = spec
                    UnregisterHotKey(probe, 99)
                    break
            if registered:
                check("RegisterHotKey", True, registered)
            else:
                lines.append("  SKIP  RegisterHotKey（试过的组合都被占用：%s）"
                             % "、".join(tried))

        # 4. 托盘（最脆的一层：结构体大小/字段错一位就会失败）
        if probe:
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = probe
            nid.uID = 7
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            nid.szTip = "selftest"
            added = bool(Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            if added:
                check("Shell_NotifyIcon NIM_ADD", True)
                nid2 = NOTIFYICONDATA()
                nid2.cbSize = ctypes.sizeof(NOTIFYICONDATA)
                nid2.hWnd = probe
                nid2.uID = 7
                check("Shell_NotifyIcon NIM_DELETE",
                      bool(Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid2))))
            elif has_package_identity():
                lines.append("  SKIP  Shell_NotifyIcon（MSIX 打包 Python 不支持经典托盘，"
                             "非代码缺陷；用 python.org 版本即可）")
            else:
                check("Shell_NotifyIcon NIM_ADD", False,
                      "err=%s" % ctypes.get_last_error())

        # 5. 提示条绘制（内存 DC，不弹窗）
        try:
            toast = Toast()
            toast.text = "自检：提示条绘制"
            screen_dc = GetDC(None)
            mem_dc = CreateCompatibleDC(screen_dc)
            bitmap = CreateCompatibleBitmap(screen_dc, 420, 90)
            old_bitmap = SelectObject(mem_dc, bitmap)
            try:
                toast._paint(mem_dc, 420, 90)
            finally:
                SelectObject(mem_dc, old_bitmap)
                DeleteObject(bitmap)
                DeleteDC(mem_dc)
                ReleaseDC(None, screen_dc)
            width, height = toast._measure("测量一行中文文字")
            check("提示条绘制 + 文字测量", width > 0 and height > 0,
                  "%dx%d" % (width, height))
            toast.show("自检：提示条显示正常", 400)
            toast.hide()
            check("提示条真正显示/隐藏", True)
            HANDLERS.pop(int(toast.hwnd), None)
            DestroyWindow(toast.hwnd)
        except Exception as exc:
            check("提示条绘制 + 文字测量", False, str(exc))

        # 6. 状态机（参考窗模式一键开/关）—— 这才是工具的核心行为
        try:
            class _StubToast(object):
                def show(self, *args, **kwargs):
                    pass

            app = SeeThroughWindow.__new__(SeeThroughWindow)
            app.states = {}
            app.hwnd = probe
            app.toast = _StubToast()
            app.registered_hotkeys = []
            app.tray_ok = False
            app.tray_added = False
            app.quitting = False
            app.max_through = 1
            app.settings = {"max_through": 1}
            app._foreground_target = lambda: probe

            base = get_ex_style(probe)
            app.toggle_ghost()
            on = get_ex_style(probe)
            check("参考窗模式：默认可交互（置顶+透明，不穿透）",
                  bool(on & WS_EX_LAYERED) and bool(on & WS_EX_TOPMOST)
                  and not (on & WS_EX_TRANSPARENT), hex(on))

            app.toggle_through()
            through = get_ex_style(probe)
            check("点击穿透：需要单独开启", bool(through & WS_EX_TRANSPARENT), hex(through))

            # 关键回归：默认只允许一个窗口穿透。前台换到别的窗口之后再按，
            # 热键控制的仍然是那一个，不会把穿透开到新窗口上。
            other = create_window("SeeThroughWindowSelfTest", "other", 0,
                                  WS_VISIBLE, -3000, -3160, 40, 30)
            other_base = get_ex_style(other)
            app._foreground_target = lambda: other
            app.toggle_through()
            rescued = get_ex_style(probe)
            untouched = get_ex_style(other)
            check("单一穿透：前台换窗也只关那一个，不会开到新窗口",
                  not (rescued & WS_EX_TRANSPARENT) and untouched == other_base,
                  "probe=%s other=%s" % (hex(rescued), hex(untouched)))

            # 上限已达时，再对别的窗口按热键也不能开第二个
            app.toggle_through()   # 前台是 other，此时没有窗口穿透 → 给 other 开
            both = (get_ex_style(other) & WS_EX_TRANSPARENT) != 0
            app._foreground_target = lambda: probe
            app.toggle_through()   # 前台是 probe，但已有 other 在穿透 → 仍只关 other
            reverted = get_ex_style(other)
            check("上限为 1：不会同时存在两个穿透窗口",
                  both and not (reverted & WS_EX_TRANSPARENT),
                  "other=%s" % hex(reverted))

            DestroyWindow(other)
            app._foreground_target = lambda: probe

            app.toggle_ghost()
            off = get_ex_style(probe)
            check("参考窗模式：再按还原", off == base, "%s vs %s" % (hex(off), hex(base)))
        except Exception as exc:
            check("参考窗模式状态机", False, str(exc))

        # 7. 前台窗口可枚举
        fg = GetForegroundWindow()
        check("GetForegroundWindow", bool(fg), str(int(fg or 0)))

        # 8. 控制台退出兜底
        try:
            ok = SetConsoleCtrlHandler(PHANDLER_ROUTINE(lambda event: False), True)
            check("SetConsoleCtrlHandler（退出还原兜底）", bool(ok))
        except Exception as exc:
            check("SetConsoleCtrlHandler（退出还原兜底）", False, str(exc))
        if has_package_identity():
            lines.append("  INFO  当前 Python 为 MSIX 打包版本，托盘功能不可用")

        # 9. 配置解析（用户改 config.ini 后能不能被正确读出来）
        try:
            hotkeys, problems, settings = build_hotkeys()
            check("热键配置可解析", len(hotkeys) == len(HOTKEY_DEFAULTS) and not problems,
                  "%d 项" % len(hotkeys))
            check("max-through 默认值为 1", settings["max_through"] == 1,
                  str(settings["max_through"]))

            mods, vk = parse_hotkey("ctrl+shift+alt+x")
            check("三个修饰键解析",
                  mods == (MOD_CONTROL | MOD_SHIFT | MOD_ALT) and vk == 0x58, hex(mods))
            check("快捷键显示名",
                  pretty_hotkey("ctrl+shift+alt+x") == "Ctrl+Shift+Alt+X",
                  pretty_hotkey("ctrl+shift+alt+x"))

            rejected = 0
            for bad_spec in ("ctrl+alt+", "nonsense"):
                try:
                    parse_hotkey(bad_spec)
                except ValueError:
                    rejected += 1
            check("非法配置被拒绝", rejected == 2, "%d/2" % rejected)
        except Exception as exc:
            check("热键配置解析", False, str(exc))

        # 11. 崩溃自愈：进程被强杀后，下次启动能把残留的窗口改回去
        victim = None
        try:
            victim = create_window("SeeThroughWindowSelfTest", "victim", 0,
                                   WS_VISIBLE, -3000, -3240, 40, 30)
            original_ex = get_ex_style(victim)
            write_state_file([{
                "hwnd": int(victim),
                "ex": original_ex,
                "topmost": False,
                "title": "victim",
                "class": "SeeThroughWindowSelfTest",
            }])
            check("状态文件可写入", os.path.exists(state_file_path()))

            # 模拟「程序已经被强杀，窗口停在半透明+穿透状态」
            set_ex_style(victim, original_ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            broken_ex = get_ex_style(victim)

            app2 = SeeThroughWindow.__new__(SeeThroughWindow)
            app2.states = {}
            app2.hwnd = probe
            app2.toast = type("T", (), {"show": lambda *a, **k: None})()
            recovered = app2._recover_leftovers()
            fixed_ex = get_ex_style(victim)

            check("崩溃自愈：下次启动能修好上次的残留",
                  recovered == 1 and (broken_ex & WS_EX_TRANSPARENT)
                  and fixed_ex == original_ex,
                  "%s -> %s" % (hex(broken_ex), hex(fixed_ex)))
            check("自愈后清空状态文件", not os.path.exists(state_file_path()))
        except Exception as exc:
            check("崩溃自愈", False, str(exc))
        finally:
            clear_state_file()
            if victim:
                DestroyWindow(victim)

        if probe:
            DestroyWindow(probe)

    except Exception:
        failures += 1
        lines.append("  EXCEPTION\n" + traceback.format_exc())

    lines.append("")
    lines.append("结果：" + ("全部通过" if failures == 0 else "%d 项失败" % failures))
    report = "\n".join(lines)
    print(report)

    try:
        path = os.path.join(os.environ.get("TEMP", "."), "see-through-window-selftest.log")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(report)
    except Exception:
        pass

    return 0 if failures == 0 else 1


# ============================================================== 入口


def hide_console():
    """只有「这个控制台是我们独占的」时才隐藏它。

    如果用户是从 cmd / VS Code 终端里启动的，控制台里还有别的进程，
    藏掉它会把用户的终端窗口一起弄没，也会让热键注册报告看不见。
    """
    try:
        hwnd = GetConsoleWindow()
        if not hwnd:
            return
        buffer = (DWORD * 16)()
        count = GetConsoleProcessList(buffer, 16)
        if count > 1:
            return
        ShowWindow(hwnd, SW_HIDE)
    except Exception:
        pass


_CONSOLE_GUARD_REF = None


def install_console_guard(app):
    """终端被关闭或按 Ctrl+C 时，先把窗口还原再退出。

    否则进程被强杀，目标窗口会永远停在半透明 / 点击穿透状态——而穿透状态
    下你连点都点不到它，只能重启那个程序。这个兜底比什么都重要。
    """
    global _CONSOLE_GUARD_REF

    def _on_console_event(event):
        if event in _CONSOLE_EVENTS:
            try:
                app.reset_all(quiet=True)
            except Exception:
                pass
            os._exit(0)
        return False

    _CONSOLE_GUARD_REF = PHANDLER_ROUTINE(_on_console_event)
    try:
        SetConsoleCtrlHandler(_CONSOLE_GUARD_REF, True)
    except Exception:
        pass
    return _CONSOLE_GUARD_REF


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if "--check-hotkeys" in argv:
        return check_hotkeys()
    if "--version" in argv:
        print("SeeThroughWindow 0.1.0")
        return 0

    SetProcessDPIAware()
    hide_console()

    mutex = CreateMutexW(None, True, "SeeThroughWindow.SingleInstance")
    if not mutex or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        MessageBoxW(None, "SeeThroughWindow 已经在运行。\n右键系统托盘图标可以退出。",
                    "SeeThroughWindow", 0x00000040)
        return 1

    app = SeeThroughWindow()
    install_console_guard(app)
    try:
        app.run()
    except Exception:
        MessageBoxW(None, "SeeThroughWindow 崩溃了：\n\n" + traceback.format_exc(),
                    "SeeThroughWindow", 0x00000010)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
