# -*- coding: utf-8 -*-
"""Raw Input 手柄读取

为什么需要它：
    游戏（提权运行）会把 DInput 手柄切到独占模式，
    joyGetPosEx 还能返回成功但按键永远是 0。
    Raw Input 是按进程投递的，只要本程序也是管理员，就能读到。

用法：
    rp = RawPad()
    rp.start()
    rp.snapshot()      -> {device: [bytes]}
    rp.pressed()       -> 与“静止基线”相比发生变化的位，形如 "raw:3:0x10"
"""
import ctypes
import ctypes.wintypes as w
import json
import threading
import time

WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIM_TYPEHID = 2
RIDEV_INPUTSINK = 0x00000100
HWND_MESSAGE = -3

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = w.HWND


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD),
                ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("style", w.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE),
                ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]


class RawPad:
    """一个极简的 Raw Input 监听器（只读，不注入任何输入）。"""

    def __init__(self, on_report=None):
        self.on_report = on_report
        self.lock = threading.Lock()
        self.reports = {}            # device -> bytes（最新一帧）
        self.hwnd = None
        self._ready = threading.Event()
        self._thread = None
        self._proc = None            # 防止回调被回收

    # ---------- 窗口与消息循环 ----------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                size = w.UINT(0)
                user32.GetRawInputData(w.HANDLE(lparam), RID_INPUT, None,
                                       ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = (ctypes.c_ubyte * size.value)()
                    got = user32.GetRawInputData(w.HANDLE(lparam), RID_INPUT,
                                                 ctypes.byref(buf), ctypes.byref(size),
                                                 ctypes.sizeof(RAWINPUTHEADER))
                    if got:
                        header = ctypes.cast(buf, ctypes.POINTER(RAWINPUTHEADER)).contents
                        if header.dwType == RIM_TYPEHID:
                            base = ctypes.sizeof(RAWINPUTHEADER)
                            hid_size = ctypes.c_uint32.from_buffer(buf, base).value
                            hid_count = ctypes.c_uint32.from_buffer(buf, base + 4).value
                            n = min(hid_size * max(hid_count, 1), 128)
                            data = ctypes.string_at(ctypes.addressof(buf) + base + 8, n)
                            dev = hex(header.hDevice or 0)
                            with self.lock:
                                self.reports[dev] = data
                            if self.on_report:
                                try:
                                    self.on_report(dev, data)
                                except Exception:
                                    pass
            except Exception:
                pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wnd_proc)
        cls = WNDCLASSEX()
        cls.cbSize = ctypes.sizeof(WNDCLASSEX)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "RotPilotRawPad"
        user32.RegisterClassExW(ctypes.byref(cls))

        user32.CreateWindowExW.argtypes = [
            w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        hwnd = user32.CreateWindowExW(0, "RotPilotRawPad", "rawpad", 0,
                                      0, 0, 0, 0, w.HWND(HWND_MESSAGE),
                                      None, hinst, None)
        self.hwnd = hwnd
        if not hwnd:
            return

        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT]
        for usage in (5, 4, 8):      # GamePad / Joystick / MultiAxis
            dev = RAWINPUTDEVICE(1, usage, RIDEV_INPUTSINK, hwnd)
            user32.RegisterRawInputDevices(ctypes.byref(dev), 1,
                                           ctypes.sizeof(RAWINPUTDEVICE))
        self._ready.set()

        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def start(self, timeout=2.0):
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def snapshot(self):
        with self.lock:
            return {k: list(v) for k, v in self.reports.items()}

    # ---------- 学习单个按键 ----------
    def learn(self, seconds=1.6, settle=0.25):
        """按住一个键，返回稳定的位签名：['dev:byte:0xMASK', ...]"""
        # 1) 采一段静止基线
        base_frames = []
        t0 = time.time()
        while time.time() - t0 < settle:
            snap = self.snapshot()
            if snap:
                base_frames.append(snap)
            time.sleep(0.02)
        if not base_frames:
            return []
        base = {}
        for dev in base_frames[-1]:
            frames = [f[dev] for f in base_frames if dev in f]
            n = min(len(x) for x in frames)
            base[dev] = bytes(sorted(fr[i] for fr in frames)[len(frames) // 2]
                              for i in range(n))

        # 2) 按住期间采样，统计“稳定置位”的 bit
        hits = {}
        total = 0
        t0 = time.time()
        while time.time() - t0 < seconds:
            snap = self.snapshot()
            total += 1
            for dev, data in snap.items():
                b0 = base.get(dev)
                if not b0:
                    continue
                n = min(len(b0), len(data))
                for i in range(n):
                    diff = b0[i] ^ data[i]
                    if not diff:
                        continue
                    for bit in range(8):
                        mask = 1 << bit
                        if diff & mask:
                            key = (dev, i, mask)
                            hits[key] = hits.get(key, 0) + 1
            time.sleep(0.02)
        if not total:
            return []
        out = []
        for (dev, i, mask), cnt in hits.items():
            if cnt / max(total, 1) >= 0.6:
                out.append("%s:%d:0x%02X" % (dev, i, mask))
        return sorted(out)

    # ---------- 差异检测 ----------
    def diff(self, baseline, report=None):
        """返回与基线相比置位的 bits：['raw:8:0x10', ...]"""
        out = []
        snap = report or self.snapshot()
        for dev, data in snap.items():
            base = baseline.get(dev)
            if not base:
                continue
            n = min(len(base), len(data))
            for i in range(n):
                diff = base[i] ^ data[i]
                if not diff:
                    continue
                for bit in range(8):
                    mask = 1 << bit
                    if diff & mask and (data[i] & mask):
                        out.append("raw:%s:%d:0x%02X" % (dev, i, mask))
        return out


if __name__ == "__main__":
    rp = RawPad()
    ok = rp.start()
    print("启动:", ok, "hwnd:", rp.hwnd)
    time.sleep(1)
    print("设备:", list(rp.snapshot().keys()))
    base = rp.snapshot()
    print("基线已取，5 秒内按键…")
    t0 = time.time()
    while time.time() - t0 < 5:
        d = rp.diff(base)
        if d:
            print("按下:", d)
            base = rp.snapshot()
        time.sleep(0.05)
