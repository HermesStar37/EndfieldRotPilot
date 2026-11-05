"""Recognize an explicitly calibrated restart-button click without sending input.

Only desktop pixels and public window/mouse APIs are read.  The callback runs on
the watcher thread: callers must enqueue their UI work instead of touching Tk.
"""

import base64
import ctypes
import io
import math
import ntpath
import queue
import threading
import time
from collections import deque
from ctypes import wintypes

from PIL import Image, ImageChops, ImageGrab, ImageStat


_MAX_IMAGE_SIDE = 8192
_MAX_PIXELS = 36_000_000
_TEMPLATE_SIDE = 320
_MAX_PNG_BYTES = 20_000_000
_POLL_SECONDS = 0.01
_CACHE_SECONDS = 0.35
_CAPTURE_SECONDS = 0.075
_COOLDOWN_SECONDS = 1.5
_MAX_CAPTURE_PIXELS = 4_000_000


def _exe(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("未能确定游戏进程，请先打开游戏。")
    return ntpath.basename(value.strip()).casefold()


def _size(raw):
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("截图尺寸无效，请重新标记。")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        raise ValueError("截图尺寸无效，请重新标记。")
    w, h = raw
    if min(w, h) < 32 or max(w, h) > _MAX_IMAGE_SIDE or w * h > _MAX_PIXELS:
        raise ValueError("游戏截图尺寸不受支持，请重新标记。")
    return [w, h]


def _box(raw, size):
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("请选择完整的按钮和重开文字区域。")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in raw):
        raise ValueError("标记坐标无效，请重新框选。")
    x, y, w, h = raw
    if w < 16 or h < 12:
        raise ValueError("标记区域太小，请包含完整的按钮或重开文字。")
    if x < 0 or y < 0 or x + w > size[0] or y + h > size[1]:
        raise ValueError("标记区域超出游戏截图，请重新框选。")
    return [x, y, w, h]


def _encode(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _decode(raw, *, template=False):
    if not isinstance(raw, str) or len(raw) > _MAX_PNG_BYTES * 4 // 3 + 8:
        raise ValueError("截图数据无效，请重新截取。")
    try:
        data = base64.b64decode(raw, validate=True)
        with Image.open(io.BytesIO(data)) as source:
            w, h = source.size
            limit = _TEMPLATE_SIDE if template else _MAX_IMAGE_SIDE
            if (source.format != "PNG" or min(w, h) < 1 or max(w, h) > limit
                    or w * h > _MAX_PIXELS):
                raise ValueError("截图尺寸或格式无效。")
            source.load()
            return source.convert("RGB")
    except Exception as exc:
        raise ValueError("截图数据无效，请重新截取。") from exc


def _has_detail(image):
    gray = image.convert("L")
    low, high = gray.getextrema()
    return high - low >= 24 and ImageStat.Stat(gray).stddev[0] >= 5.0


def _template_size(size):
    ratio = min(1.0, _TEMPLATE_SIDE / max(size))
    return tuple(max(1, round(v * ratio)) for v in size)


def _thumbnail(image):
    size = _template_size(image.size)
    return image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image


class _WindowsBackend:
    def __init__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise RuntimeError("自动重开识别仅支持 Windows。")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsIconic.argtypes = [wintypes.HWND]
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def now(self):
        return time.monotonic()

    def client_rect(self, hwnd):
        if not self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd):
            raise ValueError("游戏窗口不可见，请先切回游戏。")
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not self.user.GetClientRect(hwnd, ctypes.byref(rect)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise ctypes.WinError(ctypes.get_last_error())
        return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top

    def foreground_window(self):
        return self.user.GetForegroundWindow()

    def cursor_pos(self):
        point = wintypes.POINT()
        if not self.user.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return point.x, point.y

    def left_down(self):
        return bool(self.user.GetAsyncKeyState(1) & 0x8000)

    def process_exe(self, hwnd):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return ""
            return ntpath.basename(buffer.value).casefold()
        finally:
            self.kernel.CloseHandle(handle)

    def grab(self, bounds):
        # all_screens is required for games on monitors left/above the primary.
        return ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")


def capture_game(game, *, backend=None):
    """Capture the sure game window's client area; return an ephemeral snapshot."""
    if not isinstance(game, dict) or game.get("sure") is not True or not game.get("hwnd"):
        raise ValueError("未能确定游戏窗口，请先打开《明日方舟：终末地》。")
    backend = backend or _WindowsBackend()
    exe = _exe(game.get("exe"))
    if backend.foreground_window() != game["hwnd"]:
        raise ValueError("请先切回游戏，并保持重开确认画面在最前方。")
    if _exe(backend.process_exe(game["hwnd"])) != exe:
        raise ValueError("游戏进程已改变，请重新截图。")
    left, top, w, h = backend.client_rect(game["hwnd"])
    size = _size([w, h])
    image = backend.grab((left, top, left + w, top + h))
    if (backend.foreground_window() != game["hwnd"]
            or backend.client_rect(game["hwnd"]) != (left, top, w, h)):
        raise ValueError("截图期间游戏窗口发生变化，请重新截图。")
    if list(image.size) != size or not _has_detail(image):
        raise ValueError("游戏截图为空白或黑屏，请切回游戏后重新截图。")
    return {"exe": exe, "size": size, "image": _encode(image), "capturedAt": time.time()}


def make_target(snapshot, button_box, context_box):
    """Create a config containing only the two selected local image templates."""
    if not isinstance(snapshot, dict):
        raise ValueError("请先截取游戏画面。")
    exe, size = _exe(snapshot.get("exe")), _size(snapshot.get("size"))
    image = _decode(snapshot.get("image"))
    if list(image.size) != size or not _has_detail(image):
        raise ValueError("游戏截图为空白或尺寸无效，请重新截图。")
    target = {"enabled": True, "exe": exe, "size": size}
    for key, raw in (("button", button_box), ("context", context_box)):
        x, y, w, h = box = _box(raw, size)
        crop = image.crop((x, y, x + w, y + h))
        if not _has_detail(crop):
            raise ValueError("标记区域为纯色或缺少文字，请包含完整按钮和独特的重开说明。")
        target[key] = {"box": box, "image": _encode(_thumbnail(crop))}
    return validate_target(target)


def validate_target(raw):
    """Return a normalized config, raising ValueError for malformed calibration."""
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
        raise ValueError("自动重开标记配置无效，请重新标记。")
    size = _size(raw.get("size"))
    target = {"enabled": raw["enabled"], "exe": _exe(raw.get("exe")), "size": size}
    for key in ("button", "context"):
        entry = raw.get(key)
        if not isinstance(entry, dict):
            raise ValueError("按钮和重开说明都需要标记。")
        box = _box(entry.get("box"), size)
        image = _decode(entry.get("image"), template=True)
        expected = _template_size((box[2], box[3]))
        if image.size != expected or not _has_detail(image):
            raise ValueError("标记模板无效，请重新标记按钮和重开说明。")
        target[key] = {"box": box, "image": _encode(image)}
    return target


def _matches(image, template):
    if not _has_detail(image):
        return False
    image = image.convert("RGB").resize(template.size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(image, template)
    if sum(ImageStat.Stat(difference).mean) / 3 > 16:
        return False
    histogram = difference.convert("L").histogram()
    if sum(histogram[41:]) / (image.width * image.height) > 0.12:
        return False
    # Dialogs often share a large, identical dark background. Give contrasting
    # text/marks their own budget so a different dialog cannot pass merely
    # because most of its background pixels match. Include candidate marks too,
    # catching additional words as well as missing words.
    masks = []
    for source in (image, template):
        gray = source.convert("L")
        counts, accumulated, median = gray.histogram(), 0, 0
        halfway = source.width * source.height // 2
        for value, count in enumerate(counts):
            accumulated += count
            if accumulated > halfway:
                median = value
                break
        masks.append(gray.point([255 if abs(value - median) >= 30 else 0
                                 for value in range(256)]))
    mask = ImageChops.lighter(*masks)
    salient = difference.convert("L").histogram(mask=mask)
    count = sum(salient)
    if count < 8:
        return False
    return (sum(value * frequency for value, frequency in enumerate(salient)) / count <= 24
            and sum(salient[41:]) / count <= 0.22)


def _inside(point, box, margin=0):
    x, y, w, h = box
    return x - margin <= point[0] < x + w + margin and y - margin <= point[1] < y + h + margin


def _contrast_shape(image):
    """Preserve contrasting glyph geometry through button hover/inversion."""
    gray = image.convert("L")
    accumulated, median = 0, 0
    for value, count in enumerate(gray.histogram()):
        accumulated += count
        if accumulated > gray.width * gray.height // 2:
            median = value
            break
    low, high = gray.getextrema()
    contrast = max(median - low, high - median)
    if contrast < 35:
        return None
    return gray.point([min(255, round(abs(value - median) * 255 / contrast))
                       for value in range(256)]).convert("RGB")


def _matches_button(image, template):
    if _matches(image, template):
        return True
    # Hover can invert the button's background and lettering. Normalize only
    # this region, then retain the same strict salient-pixel comparison. Context
    # deliberately keeps its original appearance and strict RGB comparison.
    # Resample before normalization so glyph antialiasing from a uniformly
    # scaled game window is compared at the same resolution and contrast.
    resized = image.convert("RGB").resize(template.size, Image.Resampling.LANCZOS)
    candidate, reference = _contrast_shape(resized), _contrast_shape(template)
    return candidate is not None and reference is not None and _matches(candidate, reference)


class WindowsMouseEventSource:
    """Queue Windows mouse events independently of slow desktop captures.

    callback receives kind/down/up/move, original monotonic at, screen pos and
    foreground HWND. It must only enqueue: no screenshots or process queries.
    """
    def __init__(self):
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None
        self._thread_id = 0

    @staticmethod
    def _event_time(raw_ms, tick_ms, now):
        # MSLLHOOKSTRUCT.time wraps every 49.7 days; signed modular subtraction
        # maps its recent past onto GetTickCount64 even across that boundary.
        age_ms = ((int(tick_ms) - int(raw_ms) + 0x80000000) & 0xffffffff) - 0x80000000
        return now - max(0, age_ms) / 1000.0

    def start(self, callback):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, args=(callback,),
                                        name="restart-mouse-hook", daemon=True)
        self._thread.start()
        if not self._ready.wait(3):
            self.stop()
            raise RuntimeError("鼠标点击监听启动超时。")
        if self._error:
            self.stop()
            raise RuntimeError("鼠标点击监听启动失败：" + str(self._error))

    def stop(self):
        self._stop.set()
        if self._thread_id and hasattr(self, "_user"):
            self._user.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def _run(self, callback):
        hook = None
        try:
            user = self._user = ctypes.WinDLL("user32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            result_type = ctypes.c_ssize_t
            hook_type = ctypes.WINFUNCTYPE(result_type, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

            class MouseInfo(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("extraInfo", ctypes.c_size_t)]

            user.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_type, wintypes.HINSTANCE, wintypes.DWORD]
            user.SetWindowsHookExW.restype = wintypes.HANDLE
            user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
            user.CallNextHookEx.restype = result_type
            user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
            user.GetForegroundWindow.restype = wintypes.HWND
            user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel.GetModuleHandleW.restype = wintypes.HMODULE
            kernel.GetTickCount64.restype = ctypes.c_ulonglong
            self._thread_id = kernel.GetCurrentThreadId()

            @hook_type
            def proc(code, message, pointer):
                if code >= 0 and not self._stop.is_set() and message in (0x0200, 0x0201, 0x0202):
                    try:
                        info = ctypes.cast(pointer, ctypes.POINTER(MouseInfo)).contents
                        callback({"kind": {0x0200: "move", 0x0201: "down", 0x0202: "up"}[message],
                                  "at": self._event_time(info.time, kernel.GetTickCount64(), time.monotonic()),
                                  "pos": (info.pt.x, info.pt.y),
                                  "foreground": user.GetForegroundWindow()})
                    except Exception:
                        pass  # A hook must always return promptly to Windows.
                return user.CallNextHookEx(hook, code, message, pointer)

            self._hook_proc = proc  # Keep the native callback alive until unhooked.
            msg = wintypes.MSG()
            user.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # Create the queue before stop can post.
            hook = user.SetWindowsHookExW(14, proc, kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            while not self._stop.is_set():
                result = user.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(msg))
                user.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self._error = exc
        finally:
            if hook:
                self._user.UnhookWindowsHookEx(hook)
            self._thread_id = 0
            self._ready.set()


class RestartButtonWatcher:
    """Match pre-click evidence while an independent source records full clicks.

    The 350 ms evidence window covers one desktop grab plus a normal click while
    retaining its real capture-start timestamp. on_restart must only queue UI work.
    Tests may inject backend and event_source with the same monotonic clock.
    """
    def __init__(self, target, on_restart, find_game, log, *, backend=None, event_source=None):
        self.target = validate_target(target)
        self.on_restart, self.find_game, self.log = on_restart, find_game, log
        self.backend = backend or _WindowsBackend()
        self.event_source = event_source or WindowsMouseEventSource()
        self.templates = {key: _decode(self.target[key]["image"], template=True)
                          for key in ("button", "context")}
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._events = queue.Queue(maxsize=1024)
        self._overflow = threading.Event()
        self._history = deque(maxlen=16)
        self._armed = None
        self._thread = self._input_thread = None
        self._last_restart = self._last_log = self._last_reject_log = -math.inf
        self._status = {"state": "stopped", "message": "未开启点击监听"}
        self._capture_interval = 0.20

    def status(self):
        with self._lock:
            return dict(self._status)

    def _state(self, state, message, **extra):
        with self._lock:
            if self._stop.is_set() and state != "stopped":
                return
            # Keep success visible briefly instead of immediately replacing it
            # with the loading screen's expected mismatch/background state.
            if (self._status["state"] == "restarted" and state not in ("error", "stopped")
                    and self.backend.now() - self._last_restart < 1):
                self._status.update(extra)
                return
            self._status.update(state=state, message=message, **extra)

    def _enqueue(self, event):
        if not self._stop.is_set():
            try:
                self._events.put_nowait(event)
            except queue.Full:
                self._overflow.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            if self._stop.is_set():
                raise RuntimeError("重开识别线程仍在退出，请稍后重新开启技能轴。")
            return self
        if not self.target["enabled"]:
            self._state("stopped", "自动重开已关闭")
            return self
        self._stop.clear()
        with self._lock:
            self._history.clear()
            self._armed = None
            self._last_restart = -math.inf
            self._status = {"state": "waiting_game", "message": "等待确定的游戏窗口"}
        self._events = queue.Queue(maxsize=1024)
        self._overflow.clear()
        try:
            self.event_source.start(self._enqueue)
            self._input_thread = threading.Thread(target=self._input_loop, name="restart-clicks", daemon=True)
            self._thread = threading.Thread(target=self._capture_loop, name="restart-capture", daemon=True)
            self._input_thread.start()
            self._thread.start()
        except Exception:
            self.stop()
            raise
        return self

    def stop(self):
        self._stop.set()
        self.event_source.stop()
        for thread in (self._input_thread, self._thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=3)
        with self._lock:
            self._history.clear()
            self._armed = None
            self._status.update(state="stopped", message="点击监听已停止")
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except queue.Empty:
                break

    def _warn(self, message):
        now = self.backend.now()
        if now - self._last_log >= 10:
            self._last_log = now
            self.log("自动重开识别：" + message)

    def _geometry(self, game):
        left, top, w, h = rect = tuple(self.backend.client_rect(game["hwnd"]))
        if min(w, h) <= 0:
            raise ValueError("游戏窗口尺寸无效")
        base_w, base_h = self.target["size"]
        sx, sy = w / base_w, h / base_h
        if abs(sx - sy) > max(sx, sy) * 0.005:
            raise ValueError("游戏宽高比已改变，请重新标记重开按钮")
        boxes = {}
        for name in ("button", "context"):
            x, y, bw, bh = self.target[name]["box"]
            x1, y1 = round(x * sx), round(y * sy)
            x2, y2 = round((x + bw) * sx), round((y + bh) * sy)
            if min(x2 - x1, y2 - y1) < 2:
                raise ValueError("游戏窗口过小，请恢复尺寸或重新标记")
            boxes[name] = (left + x1, top + y1, x2 - x1, y2 - y1)
        return (game["hwnd"], rect), boxes

    def _context(self):
        game = self.find_game()
        if (not isinstance(game, dict) or game.get("sure") is not True
                or not game.get("hwnd") or _exe(game.get("exe")) != self.target["exe"]):
            return None, "waiting_game", "等待已标记的游戏窗口"
        hwnd = game["hwnd"]
        if self.backend.foreground_window() != hwnd:
            return None, "background", "游戏未在前台，已暂停识别"
        if _exe(self.backend.process_exe(hwnd)) != self.target["exe"]:
            return None, "waiting_game", "前台进程与标记的游戏不符"
        return self._geometry(game), None, None

    def _capture_once(self):
        context, state, message = self._context()
        if context is None:
            with self._lock:
                self._history.clear()
                self._armed = None
            self._state(state, message)
            return
        key, boxes = context
        position = self.backend.cursor_pos()
        near = _inside(position, boxes["button"], margin=32)
        self._capture_interval = _CAPTURE_SECONDS if near else 0.20
        if not near:
            self._state("waiting_button", "等待鼠标靠近已标记的重开按钮")
        if self.backend.left_down():
            return  # Never treat a pressed-button image as pre-click evidence.
        left = min(box[0] for box in boxes.values())
        top = min(box[1] for box in boxes.values())
        right = max(box[0] + box[2] for box in boxes.values())
        bottom = max(box[1] + box[3] for box in boxes.values())
        if (right - left) * (bottom - top) > _MAX_CAPTURE_PIXELS:
            raise ValueError("两个标记区域跨度过大，请缩小到按钮和独特的重开文字")
        self._state("matching", "正在核对按钮和重开文字")
        began = self.backend.now()
        image = self.backend.grab((left, top, right, bottom))
        completed = self.backend.now()
        if (image.size != (right - left, bottom - top)
                or self.backend.foreground_window() != key[0]
                or tuple(self.backend.client_rect(key[0])) != key[1]):
            with self._lock:
                self._history.clear()
                self._armed = None
            self._state("background", "截图期间游戏窗口或尺寸发生变化")
            return
        matches = {}
        for name, (x, y, w, h) in boxes.items():
            crop = image.crop((x - left, y - top, x - left + w, y - top + h))
            matcher = _matches_button if name == "button" else _matches
            matches[name] = matcher(crop, self.templates[name])
        # A capture can overlap a click; completedAt ensures it cannot be used
        # as evidence for a press which happened before it had completed.
        evidence = {"key": key, "boxes": boxes, "capturedStart": began,
                    "completedAt": completed, "matched": all(matches.values())}
        with self._lock:
            if not self._stop.is_set():
                self._history.append(evidence)  # Keep negative evidence too.
        elapsed = round((completed - began) * 1000, 1)
        if evidence["matched"]:
            self._state("ready", "按钮和重开文字已匹配，等待点击", captureMs=elapsed)
        else:
            failed = "按钮" if not matches["button"] else "重开文字"
            self._state("mismatch", failed + "与标记画面不匹配", captureMs=elapsed)

    def _reject(self, reason, at):
        self._armed = None
        self._state("mismatch", reason, lastClick=reason)
        if at - self._last_reject_log >= 2:
            self._last_reject_log = at
            self.log("restart button ignored: " + reason)

    def _handle_event(self, event):
        if self._stop.is_set() or not self.target["enabled"]:
            return
        kind, at, position = event["kind"], event["at"], event["pos"]
        if kind == "move":
            with self._lock:
                if self._armed and (event["foreground"] != self._armed["key"][0]
                                    or not _inside(position, self._armed["box"])):
                    self._reject("按住时鼠标移出了按钮或游戏失去前台", at)
            return
        context, state, message = self._context()
        if context is None:
            with self._lock:
                self._armed = None
            self._state(state, message, lastClick=message)
            return
        key, boxes = context
        if event["foreground"] != key[0] or not _inside(position, boxes["button"]):
            with self._lock:
                self._armed = None
            return
        with self._lock:
            if kind == "down":
                self._armed = None
                eligible = [item for item in self._history if item["completedAt"] <= at]
                evidence = eligible[-1] if eligible else None
                if evidence is None:
                    self._reject("点击前尚无完整的按钮和文字截图，请在按钮上稍停片刻", at)
                elif evidence["key"] != key:
                    self._reject("游戏位置或尺寸已改变，等待重新匹配", at)
                elif not 0 <= at - evidence["capturedStart"] <= _CACHE_SECONDS:
                    self._reject("点击前的匹配画面已过期，等待新截图", at)
                elif not evidence["matched"]:
                    self._reject("点击前的按钮或重开文字与标记不匹配", at)
                elif at - self._last_restart >= _COOLDOWN_SECONDS:
                    self._armed = {"key": key, "box": boxes["button"], "at": at}
            elif kind == "up":
                armed, self._armed = self._armed, None
                if (armed and armed["key"] == key and at >= armed["at"]
                        and at - self._last_restart >= _COOLDOWN_SECONDS and not self._stop.is_set()):
                    self._last_restart = at
                    self._state("restarted", "已识别重开点击，技能轴已从头开始",
                                lastClick="已识别完整的重开按钮点击", lastRestartAt=time.time())
                    self.log("restart button matched: complete click with fresh pre-click button/context")
                    self.on_restart()

    def _capture_loop(self):
        while not self._stop.is_set():
            began = self.backend.now()
            try:
                self._capture_once()
            except Exception as exc:
                with self._lock:
                    self._history.clear()
                    self._armed = None
                self._state("error", str(exc))
                try:
                    self._warn(str(exc))
                except Exception:
                    pass
            # Foreground prefetch lets a quick move+click use existing evidence.
            # Measure cadence from start, leaving at least 25 ms after slow grabs.
            delay = max(0.025, self._capture_interval - (self.backend.now() - began))
            self._stop.wait(delay)

    def _input_loop(self):
        while not self._stop.is_set():
            try:
                event = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if self._overflow.is_set():
                    with self._lock:
                        self._history.clear()
                        self._armed = None
                    self._overflow.clear()
                    self._state("error", "鼠标事件过多，已取消本次识别，等待重新匹配")
                    while not self._events.empty():
                        self._events.get_nowait()
                    continue
                self._handle_event(event)
            except Exception as exc:
                with self._lock:
                    self._armed = None
                self._state("error", str(exc))
                try:
                    self._warn(str(exc))
                except Exception:
                    pass
