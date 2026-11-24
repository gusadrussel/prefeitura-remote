"""Windows notification area integration on the Tk message thread."""

import ctypes
from ctypes import wintypes as w
import logging


_CALLBACK = 0x8001
_RESULT = ctypes.c_ssize_t
_WindowProc = ctypes.WINFUNCTYPE(_RESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class _NotifyIcon(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD), ("window", w.HWND), ("id", w.UINT),
        ("flags", w.UINT), ("callback", w.UINT), ("icon", w.HICON),
        ("tooltip", w.WCHAR * 128),
        ("state", w.DWORD), ("state_mask", w.DWORD), ("info", w.WCHAR * 256),
        ("version", w.UINT), ("info_title", w.WCHAR * 64), ("info_flags", w.DWORD),
        ("guid", ctypes.c_byte * 16), ("balloon_icon", w.HICON),
    ]


def _bind(dll, name, result, *args):
    function = getattr(dll, name)
    function.restype = result
    function.argtypes = args
    return function


class WindowsTray:
    def __init__(self, root, icon_path, menu_items):
        self.root = root
        self.menu_items = menu_items
        self.logger = logging.getLogger("prefeitura_remote.diagnostics")
        user = ctypes.WinDLL("user32", use_last_error=True)
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.notify = _bind(shell, "Shell_NotifyIconW", w.BOOL, w.DWORD, ctypes.POINTER(_NotifyIcon))
        self.set_proc = _bind(user, "SetWindowLongPtrW", _RESULT, w.HWND, ctypes.c_int, _RESULT)
        self.call_proc = _bind(user, "CallWindowProcW", _RESULT, _RESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.get_parent = _bind(user, "GetParent", w.HWND, w.HWND)
        self.destroy_icon = _bind(user, "DestroyIcon", w.BOOL, w.HICON)
        self.popup = _bind(user, "CreatePopupMenu", w.HMENU)
        self.append = _bind(user, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, w.WPARAM, w.LPCWSTR)
        self.track = _bind(user, "TrackPopupMenu", w.UINT, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p)
        self.cursor = _bind(user, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
        self.foreground = _bind(user, "SetForegroundWindow", w.BOOL, w.HWND)
        self.destroy_menu = _bind(user, "DestroyMenu", w.BOOL, w.HMENU)
        self.post_message = _bind(user, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        register = _bind(user, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
        load_image = _bind(user, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)

        root.update_idletasks()
        window = self.get_parent(root.winfo_id())
        icon = load_image(None, str(icon_path), 1, 0, 0, 0x0050)
        if not window or not icon:
            if icon:
                self.destroy_icon(icon)
            raise OSError("Ícone ou janela da bandeja indisponível")
        self.data = _NotifyIcon(ctypes.sizeof(_NotifyIcon), window, 1, 7, _CALLBACK, icon, "Vyntra")
        self.taskbar_created = register("TaskbarCreated")
        self.window_proc = _WindowProc(self._window_message)
        self.original_proc = self.set_proc(window, -4, ctypes.cast(self.window_proc, ctypes.c_void_p).value)
        if not self.original_proc or not self.notify(0, ctypes.byref(self.data)):
            self.close()
            raise OSError("Não foi possível registrar a bandeja")

    def _window_message(self, window, message, wparam, lparam):
        try:
            if message == self.taskbar_created and self.taskbar_created:
                if not self.notify(0, ctypes.byref(self.data)):
                    self.root.after_idle(self.root.deiconify)
            if message == _CALLBACK and wparam == self.data.id:
                if lparam in (0x0202, 0x0205):
                    self.root.after_idle(self._show_menu)
                return 0
        except Exception:
            self.logger.exception("Falha ao processar mensagem da bandeja")
        return self.call_proc(self.original_proc, window, message, wparam, lparam)

    def _show_menu(self):
        menu = self.popup()
        if not menu:
            self.root.deiconify()
            return
        try:
            actions = {}
            for index, (label, action) in enumerate(self.menu_items(), 1):
                actions[index] = action
                self.append(menu, 0 if action else 3, index, label)
            point = w.POINT()
            if self.cursor(ctypes.byref(point)):
                self.foreground(self.data.window)
                selected = self.track(menu, 0x0102, point.x, point.y, 0, self.data.window, None)
                self.post_message(self.data.window, 0, 0, 0)
                action = actions.get(selected)
                if action:
                    self.root.after_idle(action)
        finally:
            self.destroy_menu(menu)

    def close(self):
        self.notify(2, ctypes.byref(self.data))
        if self.original_proc:
            self.set_proc(self.data.window, -4, self.original_proc)
        self.destroy_icon(self.data.icon)
