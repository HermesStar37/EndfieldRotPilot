# -*- coding: utf-8 -*-
"""
技能轴 v2 · 单窗口版

一个窗口两种状态：
  编辑态 = 顶部配队 + 动作按钮 + 序列列表
  运行态 = 只留横向时间轴（置顶、可拖动）
F8 回到编辑态。

输入：开机装一次 keyboard.hook，回调里 root.after —— 和探测脚本同款。
"""

import ctypes
import io
import base64
import gzip
import json
import os
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk
import keyboard

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config.json")
GLYPH = os.path.join(BASE, "glyph")
GEN = os.path.join(BASE, "gen")
LOG = os.path.join(BASE, "run.log")

BG_EDIT = "#161a22"
BG_RUN = "#0e1117"
CARD = "#1a1f29"
CARD_DONE = "#141820"
CARD_NOW = "#24304a"
FG = "#e8ecf3"
DIM = "#4d5666"
SUB = "#9aa6b8"

# 属性 → 颜色
ELEMENT_COLORS = {
    "寒冷": (32, 192, 208),    # 冰蓝
    "自然": (192, 224, 80),    # 黄绿
    "电磁": (240, 192, 0),     # 金黄
    "灼热": (240, 96, 64),     # 橙红
    "物理": (110, 114, 120),   # 灰
}

ELEM_DIR = os.path.join(BASE, "elements")
CLASS_DIR = os.path.join(BASE, "classes")

COLORS = {
    "冰": (79, 195, 247),     # 天蓝
    "自然": (140, 198, 0),   # 亮黄绿
    "火": (239, 83, 80),      # 红
    "电": (255, 213, 79),     # 黄
    "物理": (74, 78, 87),     # 灰黑
}

COLORS = {"1": "#4ea1ff", "2": "#b06bff", "3": "#ffa94d", "4": "#3ddc84", "E": "#ff6b9d"}

_IME_LIST = []          # [(hwnd, hkl)] 记下被我们改成英文的窗口
_IME_LAST = {"hwnd": None}


def _ime_udll():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetKeyboardLayout.argtypes = [ctypes.c_uint]
    u.GetKeyboardLayout.restype = ctypes.c_void_p
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    u.GetWindowThreadProcessId.restype = ctypes.c_ulong
    u.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    u.LoadKeyboardLayoutW.restype = ctypes.c_void_p
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_void_p]
    u.IsWindow.argtypes = [ctypes.c_void_p]
    u.IsWindow.restype = ctypes.c_bool
    return u


_IME_SAVED = {"hkl": None}


def _ime_save_current():
    """记下当前前台窗口的输入法，结束时恢复。"""
    try:
        u = _ime_udll()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return
        tid = u.GetWindowThreadProcessId(hwnd, None)
        _IME_SAVED["hkl"] = u.GetKeyboardLayout(tid)
        log("ime saved %s" % _IME_SAVED["hkl"])
    except Exception as e:
        log("ime save fail " + str(e))


def _ime_restore_last():
    """把前台窗口切回运行前记下的输入法。"""
    hkl = _IME_SAVED.get("hkl")
    if not hkl:
        return
    try:
        u = _ime_udll()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return
        u.PostMessageW(hwnd, 0x0050, 0, ctypes.c_void_p(hkl))
        log("ime restored %s" % hkl)
    except Exception as e:
        log("ime restore fail " + str(e))


def _ime_watch():
    """运行期间：只要前台窗口变了，就把它切成英文并记下来。"""
    try:
        u = _ime_udll()
        hwnd = u.GetForegroundWindow()
        if not hwnd or hwnd == _IME_LAST["hwnd"]:
            return
        for h, _ in _IME_LIST:
            if h == hwnd:
                _IME_LAST["hwnd"] = hwnd
                return
        tid = u.GetWindowThreadProcessId(hwnd, None)
        hkl = u.GetKeyboardLayout(tid)
        en = u.LoadKeyboardLayoutW("00000409", 0x00000001)
        if not en:
            return
        u.PostMessageW(hwnd, 0x0050, 0, ctypes.c_void_p(en))
        _IME_LIST.append((hwnd, hkl))
        _IME_LAST["hwnd"] = hwnd
        log("ime -> english hwnd=%s hkl=%s" % (hwnd, hkl))
    except Exception as e:
        log("ime watch fail " + str(e))


def _ime_restore_all():
    """把改过的窗口逐个恢复。"""
    try:
        u = _ime_udll()
        n = 0
        for hwnd, hkl in _IME_LIST:
            try:
                if not hkl or not u.IsWindow(hwnd):
                    continue
                u.PostMessageW(hwnd, 0x0050, 0, ctypes.c_void_p(hkl))
                n += 1
            except Exception:
                pass
        log("ime restore %d window(s)" % n)
    except Exception as e:
        log("ime restore fail " + str(e))
    finally:
        _IME_LIST.clear()
        _IME_LAST["hwnd"] = None


def _ensure_admin():
    """没有管理员权限时，用 UAC 重新拉起自己。"""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return True
    try:
        import sys
        params = " ".join('"%s"' % a for a in sys.argv[1:])
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, ('"%s" %s' % (os.path.abspath(__file__), params)).strip(), BASE, 1)
        if r and r > 32:
            return False          # 已拉起提权实例，本进程退出
        log("elevate refused (%s), continue without admin" % r)
        return True
    except Exception as e:
        log("elevate fail " + str(e))
        return True


def _single_instance():
    """只允许一个实例，避免多个钩子互相打架。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 51234))
        s.listen(1)
        globals()["_INST"] = s     # 持有引用，进程活着就一直占用
        return True
    except Exception:
        return False


def _no_throttle():
    """关掉 Windows 对后台进程的省电节流，并提高定时器精度。"""
    try:
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        k32.GetCurrentProcess.restype = wt.HANDLE
        k32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wt.DWORD]
        k32.SetProcessInformation.restype = wt.BOOL

        class STATE(ctypes.Structure):
            _fields_ = [("Version", wt.ULONG),
                        ("ControlMask", wt.ULONG),
                        ("StateMask", wt.ULONG)]

        st = STATE()
        st.Version = 1
        st.ControlMask = 0x1     # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        st.StateMask = 0x0       # 关掉节流
        ok = k32.SetProcessInformation(k32.GetCurrentProcess(), 4,
                                       ctypes.byref(st), ctypes.sizeof(st))
        err = ctypes.get_last_error()
        k32.timeBeginPeriod(1)
        log("throttle off ok=%s err=%s" % (bool(ok), err))
    except Exception as e:
        log("throttle off fail %s" % e)


VK_MAP = {
    0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4", 0x45: "e",
    0x61: "1", 0x62: "2", 0x63: "3", 0x64: "4",
    0x77: "f8", 0x78: "f9", 0x1B: "esc",
}

def class_icon(name):
    """职业图标路径（classes/<职业>.png）"""
    if not name:
        return ""
    p = os.path.join(CLASS_DIR, f"{name}.png")
    return p if os.path.exists(p) else ""


def elem_icon(name):
    """属性图标路径（elements/<属性>.png）"""
    if not name:
        return ""
    p = os.path.join(ELEM_DIR, f"{name}.png")
    return p if os.path.exists(p) else ""


def _load_roster():
    """operators/roster.json: {"2": "祀", "3": "提弗洛斯", ...}"""
    rp = os.path.join(BASE, "operators", "roster.json")
    try:
        return json.load(io.open(rp, encoding="utf-8-sig"))
    except Exception:
        return {}


ROSTER = _load_roster()
try:
    _MAX_KEY = max([int(k) for k in ROSTER] or [35])
except Exception:
    _MAX_KEY = 35
OPS = [""] + [ROSTER.get(str(i)) for i in range(1, _MAX_KEY + 1) if ROSTER.get(str(i))]


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def load_cfg():
    if not os.path.exists(CFG):
        return {"holdMs": 280, "loop": True, "opacity": 0.92, "x": 80, "y": 300,
                "steps": [], "team": ["", "", "", ""], "teams": []}
    with open(CFG, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)
    # 首次运行（没有 freshInit 标记）：全部恢复出厂（四空、空轴、空编队库）
    if not cfg.get("freshInit"):
        cfg.update({
            "holdMs": 280,
            "opacity": 0.92,
            "position": [80, 300],
            "steps": [],
            "team": ["", "", "", ""],
            "teams": [],
            "teamInit": True,
            "freshInit": True,
        })
        try:
            save_cfg(cfg)
        except Exception:
            pass
    return cfg


def save_cfg(c):
    with open(CFG, "w", encoding="utf-8", newline="\n") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


OPDIR = os.path.join(BASE, "operators")


def op_file(op_name, name):
    if op_name:
        p = os.path.join(OPDIR, op_name, name)
        if os.path.exists(p):
            return p
    return ""


def hex2rgb(s):
    """'#8CC600' -> (140, 198, 0)"""
    s = str(s).strip().lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def op_color(op_name, fallback, kind=None):
    """干员底色优先级：
       colors.json[技能类型] > color.txt（专色） > element.txt（属性） > 默认"""
    if kind and op_name:
        cj = op_file(op_name, "colors.json")
        if cj:
            try:
                m = json.load(io.open(cj, encoding="utf-8-sig"))
                if isinstance(m, dict) and m.get(kind):
                    return hex2rgb(m[kind])
            except Exception:
                pass
    p = op_file(op_name, "color.txt")
    if p:
        try:
            s = io.open(p, encoding="utf-8").read().strip().lstrip("#")
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except Exception:
            pass
    p = op_file(op_name, "element.txt")
    if p:
        try:
            name = io.open(p, encoding="utf-8").read().strip()
            if name in ELEMENT_COLORS:
                return ELEMENT_COLORS[name]
        except Exception:
            pass
    return fallback


def op_offset(op_name):
    """operators/<名>/offset.txt：整数像素，正数下移、负数上移。"""
    p = op_file(op_name, "offset.txt")
    if p:
        try:
            return int(io.open(p, encoding="utf-8").read().strip())
        except Exception:
            pass
    return 0


def op_glyph(op_name, kind):
    """优先用干员专属技能图标，没有就退回通用符号。"""
    p = op_file(op_name, f"{kind}.png")
    return p or os.path.join(GLYPH, f"{kind}.png")


def _circle(im, diameter):
    """把图片裁成圆形并加白边。"""
    from PIL import ImageOps
    im = ImageOps.fit(im.convert("RGBA"), (diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse([0, 0, diameter - 1, diameter - 1], fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    # 白圈
    d2 = ImageDraw.Draw(out)
    d2.ellipse([1, 1, diameter - 2, diameter - 2], outline=(255, 255, 255, 240),
               width=max(3, int(diameter * 0.048)))
    return out


ASSETS = os.path.join(BASE, "assets")


def _load_asset(name):
    p = os.path.join(ASSETS, name)
    if os.path.exists(p):
        try:
            return Image.open(p).convert("RGBA")
        except Exception:
            return None
    return None


def _tinted(mask_img, color, size=None):
    """把白色遮罩染成目标色（mask_img 需已缩放好）。"""
    m = mask_img
    solid = Image.new("RGBA", m.size, color)
    out = Image.new("RGBA", m.size, (0, 0, 0, 0))
    out.paste(solid, (0, 0), m)
    return out


def _disc(color, S, full=False):
    """白圈 + 深色内圆 + 颜色层（裁剪到内圆，无缝）。"""
    if len(color) == 3:
        color = color + (255,)

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    if full:
        r_full = int(S * 0.452)
        ImageDraw.Draw(layer).ellipse(
            [S // 2 - r_full, S // 2 - r_full,
             S // 2 + r_full, S // 2 + r_full], fill=color)
    else:
        m = _load_asset("half.png")
        if m is not None:
            w = int(S * 1.08)
            h = max(1, int(m.height * (w / float(m.width))))
            m2 = m.resize((w, h), Image.LANCZOS)
            cl = _tinted(m2, color)
            layer.alpha_composite(cl, ((S - w) // 2, S // 2))

    r_in = int(S * 0.452)

    # 裁剪颜色层到内圆
    clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(clip).ellipse([S // 2 - r_in, S // 2 - r_in,
                                  S // 2 + r_in, S // 2 + r_in], fill=255)
    layer.putalpha(ImageChops.multiply(layer.split()[3], clip))

    # 组装：白圈 + 深内圆 + 颜色
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    r_out = int(S * 0.5) - 1
    d.ellipse([S // 2 - r_out, S // 2 - r_out, S // 2 + r_out, S // 2 + r_out],
              fill=(255, 255, 255, 240))
    d.ellipse([S // 2 - r_in, S // 2 - r_in, S // 2 + r_in, S // 2 + r_in],
              fill=(24, 26, 32, 255))
    out.alpha_composite(layer)
    return out


def _paste_glyph(base, glyph_path, ratio=0.5, dy=0):
    im = base.copy()
    if not os.path.exists(glyph_path):
        return im
    g = Image.open(glyph_path).convert("RGBA")
    # 裁掉透明留白，再按比例放大，视觉上才够大
    bbox = g.getbbox()
    if bbox:
        g = g.crop(bbox)
    gs = max(8, int(im.width * ratio))
    scale = gs / float(max(g.width, g.height))
    g = g.resize((max(1, int(g.width * scale)), max(1, int(g.height * scale))), Image.LANCZOS)

    # 按像素重心居中（符号本身可能上重下轻）
    a = g.split()[3]
    px = a.load()
    tot = sx = sy = 0
    for y in range(0, g.height, 2):
        for x in range(0, g.width, 2):
            v = px[x, y]
            if v:
                tot += v
                sx += x * v
                sy += y * v
    if tot:
        cx = sx / tot
        cy = sy / tot
        ox = int(round(im.width / 2.0 - cx))
        oy = int(round(im.height / 2.0 - cy))
    else:
        ox = (im.width - g.width) // 2
        oy = (im.height - g.height) // 2
    im.alpha_composite(g, (ox, oy + dy))
    return im


def icon(kind, color, label, op_name="", badge=True):
    """badge=True：带右下小图标/小头像（运行用）
       badge=False：只有圆盘和符号（编辑区按钮用）"""
    S = 256
    tag = "" if badge else "_nb"
    cache = os.path.join(GEN, f"{kind}_{op_name or 'x'}_{label or 'x'}{tag}.png")
    if os.path.exists(cache):
        return cache
    os.makedirs(GEN, exist_ok=True)

    avatar = op_file(op_name, "avatar.png")
    glyph_p = op_glyph(op_name, kind)
    if isinstance(color, str) and color.startswith("#") and len(color) == 7:
        color = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    col = op_color(op_name, tuple(color), kind)

    badge_d = int(S * 0.42)

    if kind == "link" and avatar and badge:
        main = _circle(Image.open(avatar), S - 8)
        base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        base.alpha_composite(main, (4, 4))
        small = _disc(col + (255,), badge_d)
        small = _paste_glyph(small, op_glyph(op_name, "link"), 0.8)
        bx, by = S - badge_d - 6, S - badge_d - 6
        base.alpha_composite(small, (bx, by))
    else:
        base = _disc(col + (255,), S, full=(kind == "ult"))
        base = _paste_glyph(base, glyph_p, 0.78, dy=op_offset(op_name))
        if avatar and badge:
            small = _circle(Image.open(avatar), badge_d)
            bx, by = S - badge_d - 6, S - badge_d - 6
            base.alpha_composite(small, (bx, by))

    base.resize((96, 96), Image.LANCZOS).save(cache)
    return cache


try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jieneng.Skillbar.1")
except Exception:
    pass


class App(tk.Tk):
    def __init__(self):
        if not _single_instance():
            messagebox.showwarning("已在运行", "技能轴已经开着了，别开第二个～")
            raise SystemExit
        super().__init__()
        # 窗口图标
        for cand in ("app_icon.png", "app_icon.ico"):
            fp = os.path.join(BASE, cand)
            if not os.path.exists(fp):
                continue
            try:
                if cand.endswith(".ico"):
                    self.iconbitmap(default=fp)
                else:
                    ph = ImageTk.PhotoImage(Image.open(fp))
                    self.iconphoto(True, ph)
                    self._app_icon = ph
                break
            except Exception:
                pass
        _no_throttle()
        self.cfg = load_cfg()
        self.steps = list(self.cfg.get("steps", []))
        self.teams = list(self.cfg.get("teams", []))
        self.running = False
        self.idx = 0
        self.press = {}
        self.q = queue.Queue()
        self._thumb_cache = {}
        self._seq_icon_cache = {}
        self.sel = None
        self.sel2 = None
        self._mark_item = None
        self._shift_at = None
        self._slots = []
        self._preview_sig = None
        self._last_cols = None
        self.auto_seg = True
        self.caret = None
        self._caret_item = None
        self._last_click = (None, 0.0)
        self._press_xy = (0, 0)
        self._dragging = False
        self._was_selected = False
        self._mark = None
        self._press_i = None
        self.segments = []
        self.seg = 0
        self._prev = {}
        self._keydown_at = {}
        try:
            self._u32 = ctypes.WinDLL("user32")
            self._u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self._u32.GetAsyncKeyState.restype = ctypes.c_short
        except Exception:
            self._u32 = None
        self.cards = []
        self.imgs = {}

        self.title("技能轴")
        self.geometry("1080x640+100+60")
        self.configure(bg=BG_EDIT)

        self._build_edit()
        self._build_run()
        try:
            self.run_win.bind_all("<KeyPress>", lambda e: "break")
            self.run_win.bind_all("<KeyRelease>", lambda e: "break")
        except Exception:
            pass

        # 只装这一次钩子（suppress=True：按键只给游戏，不产生系统提示音）
        try:
            keyboard.hook(self.on_key)
            log("hook installed")
            self.status.config(text="输入已就绪 ✓ 按 1/2/3/4/E 看这里", fg="#3ddc84")
        except Exception as e:
            log(f"hook fail {e}")
            self.status.config(text=f"输入失败：{e}", fg="#ff5c5c")

        def _outside_click(e):
            try:
                if e.widget is self.team_btn:
                    return
            except Exception:
                pass
            self.close_picker()
            self.close_team_panel()

        def _tk_key_sink(e):
            return "break"

        self._tk_key_sink = _tk_key_sink
        self.bind_all("<KeyPress>", _tk_key_sink)

        def _typing_key(e, fn):
            try:
                if isinstance(e.widget, (tk.Entry, tk.Text)):
                    return None
            except Exception:
                pass
            fn()
            return "break"

        try:
            self.bind_all("<Return>", lambda e: _typing_key(e, self.insert_break))
            self.bind_all("<BackSpace>", lambda e: _typing_key(e, self.backspace_seq))
            self.bind_all("<Left>", lambda e: _typing_key(e, lambda: self.caret_move(dx=-1)))
            self.bind_all("<Right>", lambda e: _typing_key(e, lambda: self.caret_move(dx=1)))
            self.bind_all("<Up>", lambda e: _typing_key(e, lambda: self.caret_move(dy=-1)))
            self.bind_all("<Down>", lambda e: _typing_key(e, lambda: self.caret_move(dy=1)))
        except Exception:
            pass

        self.bind("<Button-1>", _outside_click, add="+")
        self.bind_all("<Control-c>", self._on_ctrl_c)
        self.bind_all("<Control-v>", self._on_ctrl_v)
        self.bind_all("<Control-C>", self._on_ctrl_c)
        self.bind_all("<Control-z>", self._on_ctrl_z)
        self.bind_all("<Control-Z>", self._on_ctrl_z)
        self.bind_all("<Control-x>", self._on_ctrl_x)
        self.bind_all("<Control-X>", self._on_ctrl_x)
        self.bind_all("<Control-V>", self._on_ctrl_v)
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(10, self._pump)

    # ---------------- 编辑态 ----------------
    def _build_edit(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TButton", padding=6)

        self.edit = tk.Frame(self, bg=BG_EDIT)
        self.edit.pack(fill="both", expand=True)

        head = tk.Frame(self.edit, bg=BG_EDIT)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="技能轴", bg=BG_EDIT, fg=FG,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        tk.Label(head, text="选配队 → 加动作 → 开始运行", bg=BG_EDIT, fg=DIM,
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=10)

        team = tk.LabelFrame(self.edit, text=" 配队 ", bg=BG_EDIT, fg=SUB,
                             bd=1, relief="solid")
        team.pack(fill="x", padx=14, pady=(0, 6))
        self.tvars = []
        self.slot_labels = []
        self.act_labels = []
        self.slot_icons = []
        self.class_icons = []
        self.act_btns = []
        self._class_cache = {}
        self.pickbtns = []
        self._pop = None
        self._elem_cache = {}
        self._undo = []

        saved = list(self.cfg.get("team", ["", "", "", ""]))
        saved = ["" if (not s or s in ("（未选）", "空选", "（未选择）")) else s for s in saved]
        while len(saved) < 4:
            saved.append("")

        row = tk.Frame(team, bg=BG_EDIT)
        row.pack(fill="x", padx=6, pady=6)

        for i in range(4):
            col = tk.Frame(row, bg=BG_EDIT)
            col.pack(side="left", padx=6, anchor="n")
            hdr = tk.Frame(col, bg=BG_EDIT)
            hdr.pack(anchor="w")
            ic_e = tk.Label(hdr, bg=BG_EDIT)
            ic_e.pack(side="left")
            ic_c = tk.Label(hdr, bg=BG_EDIT)
            ic_c.pack(side="left", padx=(2, 0))
            lab = tk.Label(hdr, text=f"{i + 1} 号", bg=BG_EDIT, fg=FG,
                           font=("Microsoft YaHei UI", 9, "bold"))
            lab.pack(side="left", padx=(2, 0))
            self.slot_labels.append(lab)
            self.slot_icons.append(ic_e)
            self.class_icons.append(ic_c)

            v = tk.StringVar(value=saved[i])
            btn = tk.Button(col, textvariable=v, width=13, anchor="w",
                            bg="#10141b", fg=FG, activebackground="#1b2130",
                            activeforeground=FG, relief="flat", bd=0,
                            padx=8, pady=5, cursor="hand2",
                            font=("Microsoft YaHei UI", 10),
                            command=lambda i=i: self.open_picker(i))
            btn.pack(pady=(3, 0))
            self.tvars.append(v)
            self.pickbtns.append(btn)

        side = tk.Frame(row, bg=BG_EDIT)
        side.pack(side="left", padx=(16, 6), anchor="n")
        tk.Label(side, text=" ", bg=BG_EDIT, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        bwrap = tk.Frame(side, bg=BG_EDIT)
        bwrap.pack()
        tk.Button(bwrap, text="清空编队", bg="#2a1f22", fg="#e8b0b0",
                  activebackground="#3a2529", activeforeground="#ffd0d0",
                  relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
                  font=("Microsoft YaHei UI", 10),
                  command=self.clear_team).pack(side="left", padx=(0, 6))
        self.team_btn = tk.Button(bwrap, text="快捷编队", bg="#1b2333", fg=FG,
                                  activebackground="#243049", activeforeground=FG,
                                  relief="flat", bd=0, padx=10, pady=6,
                                  cursor="hand2", font=("Microsoft YaHei UI", 10),
                                  command=self.open_team_panel)
        self.team_btn.pack(side="left", padx=(0, 6))
        tk.Button(bwrap, text="生成分享码", bg="#1b2a22", fg="#b9e8c8",
                  activebackground="#234034", activeforeground="#d9f7e4",
                  relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
                  font=("Microsoft YaHei UI", 10),
                  command=self.share_export).pack(side="left", padx=(0, 6))
        tk.Button(bwrap, text="导入分享码", bg="#1b2333", fg="#cfe3ff",
                  activebackground="#243049", activeforeground="#e3f0ff",
                  relief="flat", bd=0, padx=10, pady=6, cursor="hand2",
                  font=("Microsoft YaHei UI", 10),
                  command=self.share_import).pack(side="left")

        body = tk.Frame(self.edit, bg=BG_EDIT)
        body.pack(fill="both", expand=True, padx=14, pady=6)

        left = tk.LabelFrame(body, text=" 动作 ", bg=BG_EDIT, fg=SUB, bd=1, relief="solid")
        left.pack(side="left", fill="y", padx=(0, 10), ipadx=4, ipady=4)
        for i in (1, 2, 3, 4):
            r = tk.Frame(left, bg=BG_EDIT)
            r.pack(fill="x", padx=8, pady=3)
            lab2 = tk.Label(r, text=f"{i} 号", width=3, bg=BG_EDIT, fg=COLORS[str(i)],
                            font=("Microsoft YaHei UI", 9, "bold"))
            lab2.pack(side="left", padx=(0, 4))
            self.act_labels.append(lab2)

            btns = []
            for kind, cmd in (("skill", lambda i=i: self.add(i, False)),
                              ("link", lambda i=i: self.add("E", False, i)),
                              ("ult", lambda i=i: self.add(i, True))):
                b = tk.Button(r, bg=BG_EDIT, activebackground=BG_EDIT,
                              relief="flat", bd=0, highlightthickness=0,
                              cursor="hand2", command=cmd)
                b.pack(side="left", padx=2)
                btns.append(b)
            self.act_btns.append(btns)
        tk.Label(left, text="按住 = 终结技 · 连携统一按 E", bg=BG_EDIT, fg=DIM,
                 font=("Microsoft YaHei UI", 8)).pack(pady=(8, 8))

        right = tk.LabelFrame(body, text=" 序列 ", bg=BG_EDIT, fg=SUB, bd=1, relief="solid")
        right.pack(side="left", fill="both", expand=True)
        seq_wrap = tk.Frame(right, bg="#10141b")
        seq_wrap.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.seq_canvas = tk.Canvas(seq_wrap, bg="#10141b", highlightthickness=0, bd=0)
        self.seq_sb = tk.Scrollbar(seq_wrap, orient="vertical", command=self.seq_canvas.yview)
        self.seq_canvas.configure(yscrollcommand=self.seq_sb.set)
        self.seq_sb.pack(side="right", fill="y")
        self.seq_canvas.pack(side="left", fill="both", expand=True)
        self.seq_canvas.bind("<Configure>", self._on_canvas_resize)
        self.seq_canvas.bind("<Button-1>", self._caret_from_pointer)
        try:
            self.seq_canvas.bind_all(
                "<MouseWheel>",
                lambda e: self.seq_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        except Exception:
            pass

        ops = tk.Frame(right, bg=BG_EDIT)
        ops.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(ops, text="保存", command=self.save, width=7).pack(side="left", padx=2)
        ttk.Button(ops, text="↶ 撤销", command=self.undo_step, width=8).pack(side="left", padx=2)
        ttk.Button(ops, text="注文本", command=self.add_note, width=8).pack(side="left", padx=2)
        for t, fn in (("◀ 左移", lambda: self.move(-1)), ("▶ 右移", lambda: self.move(1)),
                      ("删除", self.remove), ("清空", self.clear)):
            ttk.Button(ops, text=t, command=fn, width=7).pack(side="left", padx=2)
        ttk.Button(ops, text="插入分段", command=self.add_divider,
                   width=9).pack(side="left", padx=(10, 2))
        ttk.Button(ops, text="按配队重命名", command=self.rename_all,
                   width=11).pack(side="left", padx=(2, 2))

        lib = tk.LabelFrame(body, text=" 编队库 ", bg=BG_EDIT, fg=SUB, bd=1, relief="solid")
        lib.pack(side="left", fill="y", padx=(10, 0))
        lib.configure(width=200)
        self.tlib = tk.Listbox(lib, bg="#10141b", fg=FG, bd=0, highlightthickness=0,
                               width=22, font=("Microsoft YaHei UI", 10),
                               activestyle="none", selectbackground="#2a3a52")
        self.tlib.pack(fill="both", expand=True, padx=6, pady=(6, 4))
        self.tlib.bind("<Double-Button-1>", lambda e: self.load_team())
        libbtns = tk.Frame(lib, bg=BG_EDIT)
        libbtns.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(libbtns, text="保存当前", command=self.save_team, width=9).pack(pady=2)
        ttk.Button(libbtns, text="载入", command=self.load_team, width=9).pack(pady=2)
        ttk.Button(libbtns, text="删除", command=self.del_team, width=9).pack(pady=2)

        par = tk.Frame(self.edit, bg=BG_EDIT)
        par.pack(fill="x", padx=14, pady=4)
        self.v_hold = tk.StringVar(value=str(self.cfg.get("holdMs", 280)))
        self.v_alpha = tk.StringVar(value=str(self.cfg.get("opacity", 0.92)))
        tk.Label(par, text="长按(ms)", bg=BG_EDIT, fg=DIM).pack(side="left")
        tk.Entry(par, textvariable=self.v_hold, width=5).pack(side="left", padx=(4, 12))

        tk.Label(par, text="不透明度", bg=BG_EDIT, fg=DIM).pack(side="left", padx=(12, 0))
        tk.Entry(par, textvariable=self.v_alpha, width=5).pack(side="left", padx=(4, 12))
        tk.Label(par, text="（运行后可直接拖动窗口）", bg=BG_EDIT, fg=DIM,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")

        bar = tk.Frame(self.edit, bg=BG_EDIT)
        bar.pack(fill="x", padx=14, pady=(6, 12))
        ttk.Button(bar, text="▶ 开始运行", command=self.start).pack(side="left", padx=4)
        ttk.Button(bar, text="退出", command=self.quit_app).pack(side="right", padx=4)

        self.status = tk.Label(self.edit, text="", bg=BG_EDIT, fg=DIM, anchor="w",
                               font=("Microsoft YaHei UI", 8))
        self.status.pack(fill="x", padx=16, pady=(0, 8))
        self.refresh()
        self.sync_colors()
        self.refresh_act_icons()
        self.refresh_tlib()

    # ---------------- 运行态 ----------------
    def _build_run(self):
        self.run_win = tk.Toplevel(self)
        self.run_win.withdraw()
        self.run_win.overrideredirect(True)
        self.run_win.attributes("-topmost", True)
        self.run_win.configure(bg=BG_RUN)
        self.run = tk.Frame(self.run_win, bg=BG_RUN)
        self.strip = tk.Frame(self.run, bg=BG_RUN, padx=10, pady=8)
        self.strip.pack()
        self.hint = tk.Label(self.run, text="F8 回编辑 · F9 重置 · 可拖动",
                             bg=BG_RUN, fg="#3b4351", font=("Microsoft YaHei UI", 7))
        self.hint.pack(pady=(0, 4))
        self.debug = tk.Label(self.run, text="等待按键…", bg=BG_RUN, fg="#3b4351",
                              font=("Microsoft YaHei UI", 7))
        self.debug.pack(pady=(0, 6))
        # 拖动
        for w in (self.run, self.strip, self.hint, self.debug):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, e):
        self._drag = (e.x_root - self.run_win.winfo_x(),
                      e.y_root - self.run_win.winfo_y())

    def _drag_move(self, e):
        if getattr(self, "_drag", None):
            dx, dy = self._drag
            self.run_win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    # ---------------- 数据 ----------------
    def select(self, i, extend=False):
        if extend and self.sel is not None:
            self.sel2 = i
        else:
            self.sel = i
            self.sel2 = None
        self._paint_seq()

    def _range(self):
        """返回选中范围 [a, b]（含两端）。"""
        if self.sel is None:
            return None, None
        b = self.sel if self.sel2 is None else self.sel2
        return min(self.sel, b), max(self.sel, b)

    def _div_menu(self, event, i):
        """分段线的右键菜单。"""
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="重命名分段", command=lambda: self.rename_divider(i))
        m.add_command(label="删除分段", command=lambda: self.delete_divider(i))
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def rename_divider(self, i):
        """改分段名字。"""
        if not (0 <= i < len(self.steps)) or not self.steps[i].get("divider"):
            return
        cur = self.steps[i].get("name") or ""
        txt = simpledialog.askstring("重命名分段", "分段名称：", initialvalue=cur, parent=self)
        if txt is None:
            return
        self._snap()
        self.steps[i]["name"] = txt.strip()
        self.refresh()
        self.status.config(text="分段已重命名", fg="#3ddc84")

    def delete_divider(self, i):
        """删掉这条分段线。"""
        if not (0 <= i < len(self.steps)) or not self.steps[i].get("divider"):
            return
        self._snap()
        self.steps.pop(i)
        self.sel = None
        self.sel2 = None
        self.refresh()
        self.status.config(text="分段已删除", fg="#3ddc84")

    # ================= 序列区（Canvas 布局） =================
    CS_W, CS_H, CS_GAP, CS_PAD = 58, 74, 6, 12

    # ---------- 布局基础 ----------
    def _cols(self):
        try:
            w = self.seq_canvas.winfo_width() or 900
        except Exception:
            w = 900
        w = max(120, w - 26)          # 给滚动条留位置
        return max(1, int((w + self.CS_GAP) // (self.CS_W + self.CS_GAP)))

    def _xy(self, pos, cols):
        r, c = divmod(pos, cols)
        return c * (self.CS_W + self.CS_GAP), r * (self.CS_H + self.CS_GAP)

    def _on_canvas_resize(self, e=None):
        """窗口变宽/变窄时重新排版。"""
        try:
            cols = self._cols()
            if cols != getattr(self, "_last_cols", None):
                self._last_cols = cols
                self._layout()
        except Exception:
            pass
        self._seq_sync()

    def _seq_sync(self, _=None):
        try:
            cols = self._cols()
            n = len(getattr(self, "_slots", []))
            rows = (max(1, n) + cols - 1) // cols
            w = self.CS_PAD + cols * (self.CS_W + self.CS_GAP)
            h = max(1, rows * (self.CS_H + self.CS_GAP))
            self.seq_canvas.configure(scrollregion=(0, 0, w, h))
        except Exception:
            pass

    def _layout(self, slots=None):
        """按顺序排布；遇到换行符就另起一行。"""
        slots = self._slots if slots is None else slots
        cols = self._cols()
        row = col = 0
        for s in slots:
            if s.get("kind") == "br":
                row += 1
                col = 0
                continue
            x, y = self.CS_PAD + col * (self.CS_W + self.CS_GAP), row * (self.CS_H + self.CS_GAP)
            s["pos"] = (row, col)
            if s.get("kind") == "div":
                self.seq_canvas.coords(s["item"], x + self.CS_W // 2, y + self.CS_H // 2)
            else:
                self.seq_canvas.coords(s["item"], x, y)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        self._seq_sync()

    def _idx_to_rc(self, idx):
        """光标在 idx 位置时，处于第几行第几列。"""
        cols = self._cols()
        row = col = 0
        for k, s in enumerate(self.steps):
            if k >= idx:
                break
            if s.get("br"):
                row += 1
                col = 0
                continue
            col += 1
            if col >= cols:
                col = 0
                row += 1
        return row, col

    def _rc_to_idx(self, row, col):
        """第 row 行第 col 列对应的下标记。"""
        cols = self._cols()
        r = c = 0
        if row == 0 and col == 0:
            return 0
        for k, s in enumerate(self.steps):
            if r == row and c == col:
                return k
            if s.get("br"):
                r += 1
                c = 0
                continue
            c += 1
            if c >= cols:
                c = 0
                r += 1
            if (r, c) == (row, col):
                return k + 1
        return len(self.steps)

    def _step_xy(self, step_i):
        row, col = self._idx_to_rc(step_i)
        return (self.CS_PAD + col * (self.CS_W + self.CS_GAP),
                row * (self.CS_H + self.CS_GAP))

    def _draw_caret(self):
        """画光标（不闪），并保证它在可视区域内。"""
        try:
            if getattr(self, "caret", None) is None:
                self._hide_caret()
                return
            x, y = self._step_xy(self.caret)
            x = max(2, x - self.CS_GAP // 2)
            cv = self.seq_canvas
            if getattr(self, "_caret_item", None) is None:
                self._caret_item = cv.create_line(x, y + 4, x, y + self.CS_H - 4,
                                                  fill="#ffffff", width=2)
            else:
                cv.coords(self._caret_item, x, y + 4, x, y + self.CS_H - 4)
            cv.tag_raise(self._caret_item)
            self._scroll_to(y)
        except Exception as e:
            log("caret fail " + str(e))

    def _scroll_to(self, y):
        """让 y 附近可见（上下留余量）。"""
        try:
            cv = self.seq_canvas
            h = cv.winfo_height()
            if h <= 1:
                return
            bb = cv.bbox("all")
            total = max(1, bb[3] if bb else 1)
            top = cv.canvasy(0)
            bottom = top + h

            def moveto(f):
                f = max(0.0, min(1.0, f))
                cv.yview_moveto(f)

            if y < top + 8:
                moveto((y - 10) / total)
            elif y + self.CS_H > bottom - 8:
                moveto((y + self.CS_H + 10 - h) / total)
        except Exception as e:
            log("scroll fail " + str(e))

    def _hide_caret(self):
        try:
            if getattr(self, "_caret_item", None) is not None:
                self.seq_canvas.delete(self._caret_item)
                self._caret_item = None
        except Exception:
            pass

    def _caret_from_pointer(self, event=None):
        """点击空白/缝隙 → 光标落到最近的插入点（行首也可以）。"""
        try:
            cv = self.seq_canvas
            if event is not None:
                cx = cv.canvasx(event.x)
                cy = cv.canvasy(event.y)
            else:
                px, py = self.winfo_pointerxy()
                cx = cv.canvasx(px - cv.winfo_rootx())
                cy = cv.canvasy(py - cv.winfo_rooty())
            cols = self._cols()
            row = max(0, int(cy // (self.CS_H + self.CS_GAP)))
            col = int(round((cx - self.CS_PAD - self.CS_GAP / 2) / (self.CS_W + self.CS_GAP)))
            col = max(0, min(cols, col))
            self.caret = self._rc_to_idx(row, col)
            self._draw_caret()
        except Exception as e:
            log("caret fail " + str(e))

    def _ensure_caret(self):
        if getattr(self, "caret", None) is None:
            self.caret = len(self.steps)

    def caret_move(self, dy=0, dx=0):
        """方向键：左右按位置走，上下按行列走。"""
        try:
            self._ensure_caret()
            if dx:
                self.caret = max(0, min(len(self.steps), self.caret + dx))
            if dy:
                row, col = self._idx_to_rc(self.caret)
                cols = self._cols()
                tr = max(0, row + dy)
                self.caret = self._rc_to_idx(tr, min(col, cols))
            self._draw_caret()
        except Exception as e:
            log("caret move fail " + str(e))

    def insert_break(self):
        """回车：在光标处换行。"""
        self._ensure_caret()
        self._snap()
        pos = max(0, min(self.caret, len(self.steps)))
        self.steps.insert(pos, {"br": True})
        try:
            for sl in self._slots:
                if sl["i"] >= pos:
                    sl["i"] += 1
            self._insert_slot_at(pos, pos, {"br": True})
            self._layout()
        except Exception:
            self.refresh()
        self.caret = pos + 1
        self._draw_caret()
        self.status.config(text="已换行", fg="#3ddc84")

    def backspace_seq(self):
        """退格：删光标前一个符号（技能 / 分段 / 换行）。"""
        self._ensure_caret()
        if self.caret <= 0:
            return
        idx = self.caret - 1
        self._snap()
        self.steps.pop(idx)
        try:
            self._remove_slot(idx)
            for sl in self._slots:
                if sl["i"] > idx:
                    sl["i"] -= 1
            self._renumber()
            self._layout()
            self._paint_seq()
        except Exception as e:
            log("backspace fail " + str(e))
            self.refresh()
        self.caret = idx
        self._draw_caret()
        self.status.config(text="已删除", fg="#3ddc84")

    # ---------- 构建 ----------
    def _icon_for_cell(self, s):
        team = [v.get() for v in self.tvars]
        k = str(s.get("key", "?")).upper()
        kind = "ult" if s.get("hold") else ("link" if k == "E" else "skill")
        oi = int(s.get("op", 0) or 0)
        who = team[oi - 1] if 0 < oi <= len(team) else ""
        col = op_color(who, ELEMENT_COLORS.get("物理", (110, 114, 120)), kind)
        pth = icon(kind, col, str(oi or ""), who, badge=True)
        ck = (pth, 46)
        ph = self._seq_icon_cache.get(ck)
        if ph is None:
            try:
                im = Image.open(pth).convert("RGBA")
                im.thumbnail((46, 46), Image.LANCZOS)
                ph = ImageTk.PhotoImage(im)
            except Exception:
                ph = None
            self._seq_icon_cache[ck] = ph
        return ph

    def _renumber(self):
        """重排技能序号。"""
        try:
            n = 0
            for s in self._slots:
                if s.get("kind") == "cell":
                    n += 1
                    s["num"].config(text=str(n))
        except Exception:
            pass

    def _remove_slot(self, idx):
        """按 steps 下标删掉对应槽位（画面 + 结构）。"""
        for k, s in enumerate(self._slots):
            if s["i"] == idx:
                try:
                    if s.get("kind") == "cell":
                        s["cell"].destroy()
                    self.seq_canvas.delete(s["item"])
                except Exception:
                    pass
                self._slots.pop(k)
                if s.get("kind") == "cell" and s in self.seq_cells:
                    self.seq_cells.remove(s)
                return True
        return False

    def _insert_slot_at(self, pos, step_i, s):
        """在槽位序列的第 pos 位插入一个新槽位。"""
        if s.get("br"):
            slot = {"kind": "br", "i": step_i, "pos": None}
        elif s.get("divider"):
            slot = self._add_div_slot(step_i, s)
        else:
            slot = self._append_cell(step_i, s)
        # _add_div_slot/_append_cell 是追加到末尾的，挪到正确位置
        try:
            self._slots.remove(slot)
        except ValueError:
            pass
        self._slots.insert(min(pos, len(self._slots)), slot)
        return slot

    def _add_div_slot(self, i, s):
        """分段槽位：文字样式，但和技能一样参与排布与拖动。"""
        cv = self.seq_canvas
        nm = s.get("name") or "分段"
        item = cv.create_text(0, 0, text="｜" + nm, fill=SUB, width=self.CS_W - 6,
                              font=("Microsoft YaHei UI", 8), justify="center")
        cv.tag_bind(item, "<ButtonPress-1>", lambda e, i=i: self._drag_start_seq(i, e))
        cv.tag_bind(item, "<B1-Motion>", self._drag_move_seq)
        cv.tag_bind(item, "<ButtonRelease-1>", lambda e, i=i: self._drag_end_seq(i))
        cv.tag_bind(item, "<Double-Button-1>", lambda e, i=i: self.rename_divider(i))
        cv.tag_bind(item, "<Button-3>", lambda e, i=i: self._div_menu(e, i))
        slot = {"kind": "div", "i": i, "item": item, "pos": 0}
        self._slots.append(slot)
        return slot

    def _append_cell(self, i, s):
        ph = self._icon_for_cell(s)
        cell = tk.Frame(self.seq_canvas, bg="#10141b")
        lb = tk.Label(cell, bg="#10141b")
        if ph:
            lb.config(image=ph)
            lb.image = ph
        lb.pack()
        n = len([x for x in self._slots if x["kind"] == "cell"]) + 1
        nl = tk.Label(cell, text=str(n), bg="#10141b", fg=DIM, font=("Consolas", 7))
        nl.pack()
        item = self.seq_canvas.create_window(0, 0, window=cell, anchor="nw",
                                             width=self.CS_W, height=self.CS_H)
        for w in (cell, lb, nl):
            w.bind("<ButtonPress-1>", lambda e, i=i: self._drag_start_seq(i, e))
            w.bind("<B1-Motion>", self._drag_move_seq)
            w.bind("<ButtonRelease-1>", lambda e, i=i: self._drag_end_seq(i))
        slot = {"kind": "cell", "i": i, "item": item, "cell": cell,
                "pic": lb, "num": nl, "pos": 0}
        self._slots.append(slot)
        self.seq_cells.append(slot)
        return slot

    def _auto_segment_on_wrap(self):
        """新技能换到下一行时，自动在它前面插一条分段并询问名字。"""
        try:
            if not getattr(self, "auto_seg", True):
                return
            if not self.steps or self.steps[-1].get("divider"):
                return
            cols = self._cols()
            pos = len(self._slots) - 1
            if pos == 0 or pos % cols != 0:
                return
            # 上一格不是分段才插
            prev = self.steps[-2] if len(self.steps) >= 2 else None
            if prev is not None and prev.get("divider"):
                return
            nseg = len([s for s in self.steps if s.get("divider")]) + 1
            name = simpledialog.askstring(
                "自动分段", f"换行了，起个分段名字（第 {nseg} 段）：",
                initialvalue=f"第{nseg}段", parent=self)
            if name is None:
                return
            self.steps.insert(len(self.steps) - 1,
                              {"divider": True, "name": name.strip()})
            self.refresh()
        except Exception as e:
            log("auto seg fail " + str(e))

    def refresh(self):
        if not hasattr(self, "seq_canvas"):
            return
        self.seq_canvas.delete("all")
        self._slots = []
        self.seq_cells = []
        self._mark_item = None
        self._caret_item = None
        for i, s in enumerate(self.steps):
            if s.get("br"):
                self._slots.append({"kind": "br", "i": i, "pos": None})
            elif s.get("divider"):
                self._add_div_slot(i, s)
            else:
                self._append_cell(i, s)
        self._layout()
        self._draw_caret()
        self._paint_seq()

    def _paint_seq(self):
        a, b = self._range()
        for c in getattr(self, "seq_cells", []):
            sel = (a is not None and a <= c["i"] <= b)
            bg = "#223047" if sel else "#10141b"
            c["cell"].config(bg=bg)
            c["pic"].config(bg=bg)
            c["num"].config(bg=bg, fg=(FG if sel else DIM))

    # ---------- 指针 → 槽位下标 ----------
    def _drop_slot(self):
        try:
            cv = self.seq_canvas
            px, py = self.winfo_pointerxy()
            cx = cv.canvasx(px - cv.winfo_rootx())
            cy = cv.canvasy(py - cv.winfo_rooty())
            cols = self._cols()
            row = max(0, int(cy // (self.CS_H + self.CS_GAP)))
            col = int(round((cx - self.CS_GAP / 2) / (self.CS_W + self.CS_GAP)))
            col = max(0, min(cols, col))
            idx = row * cols + col
            return max(0, min(len(self._slots), idx))
        except Exception:
            return None

    def _cell_at_pointer(self):
        idx = self._drop_slot()
        if idx is None:
            return None
        for s in self._slots:
            if s["pos"] == idx or s["pos"] == idx - 1:
                return s["i"]
        return None

    def _show_drop_mark(self):
        try:
            idx = self._drop_slot()
            if idx is None:
                self._hide_drop_mark()
                return
            cols = self._cols()
            x, y = self._xy(idx, cols)
            x -= self.CS_GAP // 2
            cv = self.seq_canvas
            if getattr(self, "_mark_item", None) is None:
                self._mark_item = cv.create_line(x, y, x, y + self.CS_H,
                                                 fill="#4ea1ff", width=3)
            else:
                cv.coords(self._mark_item, x, y, x, y + self.CS_H)
            cv.tag_raise(self._mark_item)
        except Exception as e:
            log("mark fail " + str(e))

    def _hide_drop_mark(self):
        try:
            if getattr(self, "_mark_item", None) is not None:
                self.seq_canvas.delete(self._mark_item)
                self._mark_item = None
        except Exception:
            pass

    # ---------- 拖动 ----------
    def _drag_move_seq(self, e=None):
        if e is None:
            return
        if not getattr(self, "_dragging", False):
            px, py = getattr(self, "_press_xy", (0, 0))
            if abs(e.x_root - px) + abs(e.y_root - py) < 6:
                return
            self._dragging = True
            self._make_ghost(getattr(self, "_drag_from", 0))
        self._move_ghost()
        self._show_drop_mark()
        self._preview_order()

    def _preview_order(self):
        """只改画面顺序，模型不动：拖到哪里，其余槽位让位。"""
        try:
            idx = self._drop_slot()
            if idx is None:
                return
            src_i = getattr(self, "_drag_from", None)
            src_slot = None
            for s in self._slots:
                if s["i"] == src_i:
                    src_slot = s
                    break
            if src_slot is None:
                return
            rest = [s for s in self._slots if s is not src_slot]
            # 槽位下标 → 去掉被拖项后的插入位置
            insert = min(idx, len(rest))
            order = rest[:insert] + [src_slot] + rest[insert:]
            src_pos = order.index(src_slot)
            # 被拖的那格贴在鼠标上，不参与排布
            canvas_order = [s for s in order if s is not src_slot]
            if canvas_order:
                self._layout_slots_excluding(src_slot, canvas_order)
        except Exception as e:
            log("preview fail " + str(e))

    def _layout_slots_excluding(self, exclude, order):
        cols = self._cols()
        sig = tuple(id(s) for s in order) + (cols,)
        if sig == getattr(self, "_preview_sig", None):
            return
        self._preview_sig = sig
        for pos, s in enumerate(order):
            x, y = self.CS_PAD + self._xy(pos, cols)[0], self._xy(pos, cols)[1]
            if s["kind"] == "div":
                self.seq_canvas.coords(s["item"], x + self.CS_W // 2, y + self.CS_H // 2)
            else:
                self.seq_canvas.coords(s["item"], x, y)

    def select_prev_segment(self, div_index):
        """点分段线：选中它前面的那一整段。"""
        start = 0
        for k in range(div_index - 1, -1, -1):
            if self.steps[k].get("divider"):
                start = k + 1
                break
        end = div_index - 1
        if end < start:
            self.status.config(text="这一段是空的", fg="#ffa94d")
            return
        self.sel = start
        self.sel2 = end
        self._paint_seq()
        self.status.config(text=f"已选中上一段（{end - start + 1} 个）", fg="#3ddc84")

    def copy_steps(self):
        a, b = self._range()
        if a is None:
            self.status.config(text="先选中要复制的技能", fg="#ffa94d")
            return
        self._clip = [dict(s) for s in self.steps[a:b + 1]]
        self.status.config(text=f"已复制 {len(self._clip)} 个", fg="#3ddc84")

    def cut_steps(self):
        a, b = self._range()
        if a is None:
            self.status.config(text="先选中要剪切的技能", fg="#ffa94d")
            return
        self._clip = [dict(s) for s in self.steps[a:b + 1]]
        self._snap()
        del self.steps[a:b + 1]
        self.sel = None
        self.sel2 = None
        self.refresh()
        self.status.config(text=f"已剪切 {len(self._clip)} 个", fg="#3ddc84")

    def _toggle_select(self, i):
        """同一格再点一次 → 取消选中。"""
        import time as _t
        last_i, last_t = self._last_click
        now = _t.time()
        if last_i == i and now - last_t < 0.6 and self.sel == i and self.sel2 is None:
            self.sel = None
            self.sel2 = None
            self._last_click = (None, now)
            self._paint_seq()
            return True
        self._last_click = (i, now)
        return False

    def _drag_start_seq(self, i, e):
        shift = bool(e.state & 0x1) or bool(e.state & 0x4)   # Shift 或 Ctrl
        a0, b0 = self._range()
        # 按下前这一格是否已经在选中范围里
        self._was_selected = (a0 is not None and a0 <= i <= b0)
        if shift:
            self.select(i, extend=True)
        elif not self._was_selected:
            self.select(i)
        self._drag_from = i
        self._press_xy = (e.x_root, e.y_root)
        self._dragging = False
        self._ghost = None

    def _make_ghost(self, i):
        """把当前选中的整段技能做成半透明虚影，从原位抬起。"""
        self._kill_ghost()
        a, b = self._range()
        if a is None or not (a <= i <= b):
            a = b = i

        imgs = []
        anchor = None
        for c in getattr(self, "seq_cells", []):
            if a <= c["i"] <= b:
                img = getattr(c["pic"], "image", None)
                if img is not None:
                    imgs.append(img)
                if c["i"] == i:
                    anchor = c["pic"]
        if not imgs:
            return
        if anchor is None:
            anchor = self.seq_cells[0]["pic"]

        try:
            g = tk.Toplevel(self)
            g.overrideredirect(True)
            g.attributes("-topmost", True)
            try:
                g.attributes("-alpha", 0.7)
            except Exception:
                pass
            g.configure(bg="#10141b")
            row = tk.Frame(g, bg="#10141b")
            row.pack()
            for im in imgs:
                lb = tk.Label(row, image=im, bg="#10141b")
                lb.image = im
                lb.pack(side="left", padx=2)

            # 从按下那一格的位置抬起
            x0 = anchor.winfo_rootx() - 6
            y0 = anchor.winfo_rooty() - 6
            g.geometry(f"+{x0}+{y0}")
            self._ghost = g

            px, py = self.winfo_pointerxy()
            self._ghost_off = (px - x0, py - y0)
        except Exception as e:
            log(f"ghost fail {e}")

    def _move_ghost(self):
        g = getattr(self, "_ghost", None)
        if not g:
            return
        try:
            px, py = self.winfo_pointerxy()
            ox, oy = getattr(self, "_ghost_off", (10, 10))
            g.geometry(f"+{px - ox}+{py - oy}")
        except Exception:
            pass

    def _kill_ghost(self):
        g = getattr(self, "_ghost", None)
        if g:
            try:
                g.destroy()
            except Exception:
                pass
            self._ghost = None

    def _drag_end_seq(self, j):
        self._kill_ghost()
        self._hide_drop_mark()
        src = getattr(self, "_drag_from", None)
        dragging = getattr(self, "_dragging", False)
        self._drag_from = None
        self._dragging = False

        # 没拖动 = 单击
        if not dragging:
            if src is not None and getattr(self, "_was_selected", False):
                # 点已选中的格子 -> 取消选中
                self.sel = None
                self.sel2 = None
                self._paint_seq()
            elif src is not None:
                self.select(src)
            return

        idx = self._drop_slot()
        if idx is None or src is None:
            self.refresh()
            return

        a, b = self._range()
        if a is None or not (a <= src <= b):
            a = b = src

        # 被移动的块
        block = self.steps[a:b + 1]
        rest = self.steps[:a] + self.steps[b + 1:]

        # 槽位下标里，块占了几个
        before = sum(1 for s in self._slots[:idx] if a <= s["i"] <= b)
        insert = max(0, min(len(rest), idx - before))

        self._snap()
        self.steps = rest[:insert] + block + rest[insert:]
        self.sel = insert
        self.sel2 = insert + len(block) - 1
        self.refresh()
        self.status.config(text="已移动", fg="#3ddc84")

    def paste_steps(self):
        if not self._clip:
            self.status.config(text="剪贴板是空的", fg="#ffa94d")
            return
        self._snap()
        a, b = self._range()
        pos = len(self.steps) if a is None else b + 1
        for k, s in enumerate(self._clip):
            self.steps.insert(pos + k, dict(s))
        self.sel = pos
        self.sel2 = pos + len(self._clip) - 1
        self.refresh()
        self.status.config(text=f"已粘贴 {len(self._clip)} 个", fg="#3ddc84")

    def add(self, key, hold, op=None):
        self._snap()
        k = str(key)
        if k == "E":
            oi = int(op) if op else 1
            slot = self.tvars[oi - 1].get()
            who = slot if slot and slot != "" else f"{oi} 号"
            name = f"{who} · 连携技"
            self.steps.append({"key": "E", "hold": False, "op": oi, "label": name})
        else:
            slot = self.tvars[int(k) - 1].get()
            who = slot if slot and slot != "" else f"{k} 号"
            name = f"{who} · " + ("终结技" if hold else "战技")
            self.steps.append({"key": k, "hold": bool(hold), "op": int(k), "label": name})
        # 一律插到光标处，插完光标后移（和打字一致，不重建整块）
        if getattr(self, "caret", None) is not None:
            newst = self.steps.pop()
            pos = max(0, min(self.caret, len(self.steps)))
            self.steps.insert(pos, newst)
            try:
                # 后续槽位的 i 都要 +1
                for sl in self._slots:
                    if sl["i"] >= pos:
                        sl["i"] += 1
                self._insert_slot_at(pos, pos, newst)
                self._renumber()
                self._layout()
                self._paint_seq()
                self.caret = pos + 1
                self._draw_caret()
                return
            except Exception as e:
                log("insert fail " + str(e))
                self.refresh()
                self.caret = pos + 1
                self._draw_caret()
                return

        # 没有光标时：增量追加
        try:
            if hasattr(self, "seq_canvas") and len(self._slots) == len(self.steps) - 1:
                self._append_cell(len(self.steps) - 1, self.steps[-1])
                self._layout()
                self._paint_seq()
                self._auto_segment_on_wrap()
                return
        except Exception as e:
            log("append fail " + str(e))
        self.refresh()

    def add_divider(self):
        self._snap()
        name = simpledialog.askstring("插入分段", "给这一段起个名字（可留空）：",
                                      parent=self)
        if name is None:
            return
        sel = self._sel()
        pos = len(self.steps) if sel is None else sel + 1
        self.steps.insert(pos, {"divider": True, "name": name.strip() or ""})
        self.refresh()
        self.status.config(text="已插入分段", fg="#3ddc84")

    def relabel(self, i):
        slot = (self.tvars[i].get() or "").strip()
        who = slot if slot and slot != "" else f"{i + 1} 号"
        key = str(i + 1)
        for s in self.steps:
            if str(s.get("key")) == key:
                s["label"] = f"{who} · " + ("终结技" if s.get("hold") else "战技")
            elif str(s.get("key")).upper() == "E" and int(s.get("op", 0)) == i + 1:
                s["label"] = f"{who} · 连携技"
        self.refresh()
        self.sync_colors()
        self.refresh_act_icons()
        self.status.config(text=f"{i + 1} 号 → {who}", fg="#3ddc84")

    def _op_element(self, name):
        p = op_file(name, "element.txt")
        if p:
            try:
                return io.open(p, encoding="utf-8").read().strip()
            except Exception:
                return ""
        return ""

    def open_picker(self, idx):
        """两级选择：先属性，再干员。"""
        self.close_picker()
        btn = self.pickbtns[idx]
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg="#1a1f29")
        self._pop = top

        bar = tk.Frame(top, bg="#1a1f29", cursor="hand2")
        bar.pack(fill="x", padx=6, pady=(6, 2))
        pic = tk.Label(bar, bg="#1a1f29")
        title = tk.Label(bar, text="", bg="#1a1f29", fg="#e8ecf3",
                         font=("Microsoft YaHei UI", 10, "bold"))
        back = tk.Label(bar, text="←", bg="#1a1f29", fg="#9aa6b8",
                        font=("Microsoft YaHei UI", 12, "bold"))
        title.pack(side="left", padx=(2, 4))
        back.pack(side="left")
        for w in (bar, pic, title, back):
            w.bind("<Button-1>", lambda e: self._picker_back())

        # 可滚动区域
        wrap = tk.Frame(top, bg="#10141b")
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        canvas = tk.Canvas(wrap, bg="#10141b", highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, padx=(0, 2))
        body = tk.Frame(canvas, bg="#10141b")
        canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync(_=None):
            try:
                canvas.update_idletasks()
                w = max(1, canvas.winfo_width())
                h = max(1, body.winfo_reqheight())
                canvas.itemconfigure(1, width=w)
                canvas.configure(scrollregion=(0, 0, w, h))
                if h <= canvas.winfo_height():
                    canvas.yview_moveto(0)
            except Exception:
                pass

        body.bind("<Configure>", _sync)
        canvas.bind("<Configure>", _sync)

        def top_align():
            try:
                body.update_idletasks()
                _sync()
                canvas.yview_moveto(0.0)
            except Exception:
                pass

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        canvas.bind_all("<MouseWheel>", lambda e: _wheel(e) if self._pop is top else None)

        def clear():
            for w in body.winfo_children():
                w.destroy()

        def pick_op(name):
            self.tvars[idx].set(name)
            self.close_picker()
            self.on_pick(idx)

        def show_ops(elem):
            clear()
            self._show_elems_fn = show_elems
            self._picker_level = "ops"
            title.config(text=elem or "全部干员")
            back.pack(side="left")
            ep0 = elem_icon(elem) if elem else ""
            if ep0:
                try:
                    key = ("el", elem)
                    if key not in self._elem_cache:
                        im = Image.open(ep0).convert("RGBA")
                        im.thumbnail((18, 18), Image.LANCZOS)
                        self._elem_cache[key] = ImageTk.PhotoImage(im)
                    pic.config(image=self._elem_cache[key])
                    pic.image = self._elem_cache[key]
                    pic.pack(side="left", padx=(2, 6), before=title)
                except Exception:
                    pic.pack_forget()
            else:
                pic.pack_forget()
            for w in body.winfo_children():
                w.destroy()

            row = tk.Frame(body, bg="#10141b", cursor="hand2")
            row.pack(fill="x", pady=1)
            tk.Label(row, text="（空）", bg="#10141b", fg="#9aa6b8",
                     font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", padx=6, pady=3)
            for w in (row,):
                w.bind("<Button-1>", lambda e: pick_op(""))

            names = [n for n in OPS if n and (not elem or self._op_element(n) == elem)]
            for nm in names:
                r = tk.Frame(body, bg="#10141b", cursor="hand2")
                r.pack(fill="x", pady=1)
                av = op_file(nm, "avatar.png")
                ph = None
                if av:
                    try:
                        key = ("av", nm)
                        if key not in self._elem_cache:
                            im = Image.open(av).convert("RGBA")
                            im.thumbnail((24, 24), Image.LANCZOS)
                            self._elem_cache[key] = ImageTk.PhotoImage(im)
                        ph = self._elem_cache[key]
                    except Exception:
                        ph = None
                if ph:
                    lb = tk.Label(r, image=ph, bg="#10141b")
                    lb.image = ph
                    lb.pack(side="left", padx=(6, 4), pady=2)
                tk.Label(r, text=nm, bg="#10141b", fg="#e8ecf3",
                         font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", pady=3)
                for w in r.winfo_children() + [r]:
                    w.bind("<Button-1>", lambda e, nm=nm: pick_op(nm))
            top_align()

        def show_elems():
            clear()
            self._picker_level = "elems"
            title.config(text="选择属性")
            back.pack_forget()
            pic.pack_forget()

            r0 = tk.Frame(body, bg="#10141b", cursor="hand2")
            r0.pack(fill="x", pady=1)
            tk.Label(r0, text="全部干员", bg="#10141b", fg="#9aa6b8",
                     font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", padx=6, pady=4)
            r0.bind("<Button-1>", lambda e: show_ops(""))

            for elem in ("寒冷", "自然", "电磁", "灼热", "物理"):
                r = tk.Frame(body, bg="#10141b", cursor="hand2")
                r.pack(fill="x", pady=1)
                ep = elem_icon(elem)
                if ep:
                    try:
                        key = ("el", elem)
                        if key not in self._elem_cache:
                            im = Image.open(ep).convert("RGBA")
                            im.thumbnail((20, 20), Image.LANCZOS)
                            self._elem_cache[key] = ImageTk.PhotoImage(im)
                        ph = self._elem_cache[key]
                        lb = tk.Label(r, image=ph, bg="#10141b")
                        lb.image = ph
                        lb.pack(side="left", padx=(6, 4), pady=2)
                    except Exception:
                        pass
                cnt = len([n for n in OPS if n and self._op_element(n) == elem])
                tk.Label(r, text=elem, bg="#10141b", fg="#e8ecf3",
                         font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", pady=4)
                tk.Label(r, text=f"{cnt}", bg="#10141b", fg="#5b6577",
                         font=("Consolas", 9)).pack(side="right", padx=8)
                for w in r.winfo_children() + [r]:
                    w.bind("<Button-1>", lambda e, elem=elem: show_ops(elem))
            top_align()

        # 记录回退入口
        self._show_elems_fn = show_elems
        self._picker_level = "elems"
        show_elems()
        try:
            self.update_idletasks()
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            top.geometry(f"240x380+{x}+{y}")
            top.update_idletasks()
            _sync()
            canvas.yview_moveto(0)
        except Exception:
            top.geometry("240x380+200+200")

    def _picker_back(self):
        """从干员列表返回到属性选择层。"""
        if getattr(self, "_picker_level", None) != "ops":
            return
        fn = getattr(self, "_show_elems_fn", None)
        if fn:
            try:
                fn()
            except Exception as e:
                log(f"picker back fail {e}")

    def close_picker(self):
        if self._pop is not None:
            try:
                self._pop.destroy()
            except Exception:
                pass
            self._pop = None

    def on_pick(self, i):
        """选完干员：同名只能占一个位，重复的旧位自动清空。"""
        name = (self.tvars[i].get() or "").strip()
        if name:
            for j in range(4):
                if j != i and (self.tvars[j].get() or "").strip() == name:
                    self.tvars[j].set("")
                    self.relabel(j)
        self.relabel(i)

    # ---------------- 编队库 ----------------
    def refresh_tlib(self):
        self.tlib.delete(0, "end")
        for i, item in enumerate(self.teams, 1):
            name = item.get("name") or "未命名"
            self.tlib.insert("end", f"{i}. {name}")

    def save_team(self):
        name = simpledialog.askstring("保存编队", "给这个编队起个名字：", parent=self)
        if not name:
            return
        self.save()   # 先把当前状态落盘
        item = {
            "name": name,
            "team": [v.get() for v in self.tvars],
            "steps": [dict(s) for s in self.steps],
            "holdMs": self.cfg.get("holdMs", 280),
            "loop": self.cfg.get("loop", True),
        }
        self.teams.append(item)
        self.cfg["teams"] = self.teams
        save_cfg(self.cfg)
        self.refresh_tlib()
        self.status.config(text=f"已保存编队：{name}", fg="#3ddc84")

    def load_team(self):
        sel = self.tlib.curselection()
        if not sel:
            return
        item = self.teams[sel[0]]
        team = list(item.get("team", ["", "", "", ""]))
        while len(team) < 4:
            team.append("")
        for i in range(4):
            self.tvars[i].set(team[i])
        self.steps = [dict(s) for s in item.get("steps", [])]
        self.cfg["holdMs"] = item.get("holdMs", self.cfg.get("holdMs", 280))
        self.cfg["loop"] = item.get("loop", self.cfg.get("loop", True))
        try:
            self.v_hold.set(str(self.cfg["holdMs"]))
        except Exception:
            pass
        self.refresh()
        self.sync_colors()
        self.refresh_act_icons()
        self.status.config(text=f"已载入编队：{item.get('name', '')}", fg="#3ddc84")

    def del_team(self):
        sel = self.tlib.curselection()
        if not sel:
            return
        item = self.teams.pop(sel[0])
        self.cfg["teams"] = self.teams
        save_cfg(self.cfg)
        self.refresh_tlib()
        self.status.config(text=f"已删除编队：{item.get('name', '')}", fg="#ffa94d")

    def _class_photo(self, cls):
        key = (cls,)
        if key in self._class_cache:
            return self._class_cache[key]
        ph = None
        p = class_icon(cls)
        if p:
            try:
                im = Image.open(p).convert("RGBA")
                im.thumbnail((22, 22), Image.LANCZOS)
                a = im.split()[3]
                white = Image.new("RGBA", im.size, (255, 255, 255, 0))
                white.putalpha(a)
                ph = ImageTk.PhotoImage(white)
            except Exception:
                ph = None
        self._class_cache[key] = ph
        return ph

    def _elem_photo(self, elem):
        key = (elem,)
        if key in self._elem_cache:
            return self._elem_cache[key]
        ph = None
        p = elem_icon(elem)
        if p:
            try:
                im = Image.open(p).convert("RGBA")
                im.thumbnail((22, 22), Image.LANCZOS)
                ph = ImageTk.PhotoImage(im)
            except Exception:
                ph = None
        self._elem_cache[key] = ph
        return ph

    def _typing(self):
        try:
            w = self.focus_get()
            return isinstance(w, (tk.Entry,))
        except Exception:
            return False

    def _on_ctrl_c(self, e=None):
        if self._typing() or self.running:
            return
        self.copy_steps()

    def _on_ctrl_z(self, e=None):
        if self.running:
            return
        self.undo_step()

    def _on_ctrl_x(self, e=None):
        if self._typing() or self.running:
            return
        self.cut_steps()

    def _on_ctrl_v(self, e=None):
        if self._typing() or self.running:
            return
        self.paste_steps()

    def add_note(self):
        """给选中的技能加一句提示文本（运行时显示在图标上方）。"""
        i = self._sel()
        if i is None:
            self.status.config(text="先选中一个技能", fg="#ffa94d")
            return
        if self.steps[i].get("divider"):
            self.status.config(text="分段线不能加文本", fg="#ffa94d")
            return
        cur = self.steps[i].get("note", "")
        txt = simpledialog.askstring("插入文本", "显示在技能上方的文字（留空即删除）：",
                                     initialvalue=cur, parent=self)
        if txt is None:
            return
        self._snap()
        txt = txt.strip()
        if txt:
            self.steps[i]["note"] = txt
        else:
            self.steps[i].pop("note", None)
        self.refresh()
        self.status.config(text="已更新文本", fg="#3ddc84")

    def _snap(self):
        """把当前序列存一份，供撤销。"""
        self._undo.append([dict(s) for s in self.steps])
        if len(self._undo) > 50:
            self._undo.pop(0)

    def undo_step(self):
        """撤销编辑区的上一步（加/删/移动/分段/清空）。"""
        if not self._undo:
            self.status.config(text="没有可撤销的操作", fg="#ffa94d")
            return
        self.steps = self._undo.pop()
        self.sel = None
        self.refresh()
        self.status.config(text="已撤销上一步", fg="#3ddc84")

    def refresh_act_icons(self):
        """动作区三个圆形图标（不带头像），跟随干员属性色。"""
        team = [v.get() for v in self.tvars]
        for i in range(4):
            who = team[i] if i < len(team) else ""
            btns = self.act_btns[i] if i < len(self.act_btns) else []
            for bi, kind in enumerate(("skill", "link", "ult")):
                col = op_color(who, ELEMENT_COLORS.get("物理", (110, 114, 120)), kind)
                pth = icon(kind, col, str(i + 1), who, badge=False)
                try:
                    im = Image.open(pth).convert("RGBA")
                    im.thumbnail((38, 38), Image.LANCZOS)
                    ph = ImageTk.PhotoImage(im)
                except Exception:
                    ph = None
                if bi < len(btns) and ph:
                    btns[bi].config(image=ph)
                    btns[bi].image = ph

    def op_card_img(self, name, w=76, h=99):
        """干员卡面缩略图。"""
        key = ("card", name, w, h)
        if key in self._elem_cache:
            return self._elem_cache[key]
        ph = None
        cp = os.path.join(BASE, "cards", f"{name}.png")
        if os.path.exists(cp):
            try:
                im = Image.open(cp).convert("RGBA")
                im = im.resize((w, h), Image.LANCZOS)
                ph = ImageTk.PhotoImage(im)
            except Exception as e:
                log(f"card thumb fail {e}")
        self._elem_cache[key] = ph
        return ph

    # ---------------- 分享码 ----------------
    # 格式 RP2: base85( 二进制 )
    #   ver(1) team(4) holdMs(2) count(2) steps(n) texts(...)
    #   step 1 字节：kind(3) hold(1) op(2) hasNote(1) hasName(1)
    #   kind: 0-3 = 1/2/3/4 号，4 = E（连携），5 = 分段线
    KIND2KEY = {0: "1", 1: "2", 2: "3", 3: "4", 4: "E"}
    KEY2KIND = {"1": 0, "2": 1, "3": 2, "4": 3, "E": 4}

    def _roster_index(self, name):
        try:
            return self._roster_names().index(name) + 1
        except Exception:
            return 0

    def _roster_names(self):
        if not hasattr(self, "_roster_cache"):
            self._roster_cache = [n for n in OPS if n]
        return self._roster_cache

    def _share_encode(self):
        import struct
        team = [v.get() for v in self.tvars]
        buf = bytearray()
        buf.append(2)
        for i in range(4):
            buf.append(self._roster_index(team[i]) if i < len(team) else 0)
        buf += struct.pack("<HH", int(self.cfg.get("holdMs", 280)), len(self.steps))
        texts = []
        for s in self.steps:
            if s.get("br"):
                buf.append(6 << 5)
                continue
            if s.get("divider"):
                nm = s.get("name") or ""
                has = 1 if nm else 0
                buf.append((5 << 5) | (has << 1))
                if has:
                    texts.append(nm)
                continue
            k = str(s.get("key", "1")).upper()
            kind = self.KEY2KIND.get(k, 0)
            hold = 1 if s.get("hold") else 0
            op = max(0, min(3, int(s.get("op", 1) or 1) - 1))
            note = s.get("note") or ""
            flags = (kind << 5) | (hold << 4) | (op << 2) | ((1 if note else 0) << 1)
            buf.append(flags)
            if note:
                texts.append(note)
        for tx in texts:
            raw = tx.encode("utf-8")
            buf += struct.pack("<H", len(raw)) + raw
        return "RP2:" + base64.b85encode(bytes(buf)).decode("ascii")

    def _share_decode(self, code):
        import struct
        code = (code or "").strip()
        if code.startswith("RP1:"):
            raw = gzip.decompress(base64.urlsafe_b64decode(code[4:].encode("ascii")))
            return {"_v1": True, **json.loads(raw.decode("utf-8"))}
        if code.startswith("RP2:"):
            code = code[4:]
        data = base64.b85decode(code.encode("ascii"))
        if len(data) < 9 or data[0] != 2:
            raise ValueError("版本不支持")
        team = [self._roster_names()[data[i] - 1] if data[i] else ""
                for i in range(1, 5)]
        hold_ms, cnt = struct.unpack("<HH", data[5:9])
        steps = []
        texts = []
        pos = 9
        for _ in range(cnt):
            b = data[pos]
            pos += 1
            kind = (b >> 5) & 0x7
            if kind == 6:
                steps.append({"br": True})
                continue
            hold = (b >> 4) & 0x1
            op = ((b >> 2) & 0x3) + 1
            has_note = (b >> 1) & 0x1
            has_name = (b >> 1) & 0x1 if kind == 5 else 0
            if kind == 5:
                steps.append({"divider": True, "name": "", "_wait": 1 if has_name else 0})
            else:
                steps.append({"key": self.KIND2KEY.get(kind, "1"), "hold": bool(hold),
                              "op": op, "_wait": 1 if has_note else 0})
        while pos + 2 <= len(data):
            ln = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
            try:
                texts.append(data[pos:pos + ln].decode("utf-8"))
            except Exception:
                texts.append("")
            pos += ln
        ti = 0
        for st in steps:
            if st.pop("_wait", 0) and ti < len(texts):
                if st.get("divider"):
                    st["name"] = texts[ti]
                else:
                    st["note"] = texts[ti]
                ti += 1
        return {"t": team, "h": hold_ms, "s": steps, "_v2": True}

    def _share_to_steps(self, data):
        team = list(data.get("t") or ["", "", "", ""])[:4]
        while len(team) < 4:
            team.append("")
        steps = []
        for row in data.get("s") or []:
            if not isinstance(row, dict):
                if not row:
                    continue
                if row[0] == "|":
                    steps.append({"divider": True, "name": row[1] if len(row) > 1 else ""})
                    continue
                k = str(row[0])
                hold = bool(row[1]) if len(row) > 1 else False
                op = int(row[2]) if len(row) > 2 else 0
                note = row[3] if len(row) > 3 else ""
                st = {"key": k, "hold": hold, "op": op}
                if note:
                    st["note"] = note
                steps.append(st)
                continue
            if row.get("divider"):
                steps.append({"divider": True, "name": row.get("name") or ""})
                continue
            st = {"key": str(row.get("key", "1")), "hold": bool(row.get("hold")),
                  "op": int(row.get("op", 1) or 1)}
            if row.get("note"):
                st["note"] = row["note"]
            steps.append(st)
        # 重建标签
        for st in steps:
            if st.get("divider"):
                continue
            op = st.get("op", 1)
            who = team[op - 1] if 0 < op <= 4 and team[op - 1] else f"{op} 号"
            if str(st["key"]).upper() == "E":
                st["label"] = who + " · 连携技"
            else:
                st["label"] = who + " · " + ("终结技" if st.get("hold") else "战技")
        return team, steps

    def share_export(self):
        """生成当前编队（含轴）的分享码。"""
        try:
            code = self._share_encode()
        except Exception as e:
            messagebox.showerror("生成失败", str(e))
            return

        top = tk.Toplevel(self)
        top.title("分享码")
        top.configure(bg="#161a22")
        top.geometry("580x250")
        top.attributes("-topmost", True)
        tk.Label(top, text="复制下面这串，发给对方即可一键导入：",
                 bg="#161a22", fg=FG, font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=12, pady=(12, 6))
        txt = tk.Text(top, height=6, wrap="char", bg="#10141b", fg="#cfe3ff",
                      insertbackground=FG, bd=0, font=("Consolas", 9))
        txt.pack(fill="both", expand=True, padx=12)
        txt.insert("1.0", code)
        bar = tk.Frame(top, bg="#161a22")
        bar.pack(fill="x", padx=12, pady=8)
        nsteps = len([s for s in self.steps if not s.get("divider")])
        tk.Label(bar, text=f"{len(code)} 字符 · {nsteps} 步",
                 bg="#161a22", fg=DIM, font=("Microsoft YaHei UI", 8)).pack(side="left")

        def _copy():
            try:
                self.clipboard_clear()
                self.clipboard_append(code)
                self.status.config(text="分享码已复制到剪贴板", fg="#3ddc84")
            except Exception as e:
                messagebox.showerror("复制失败", str(e))

        ttk.Button(bar, text="复制", command=_copy, width=8).pack(side="right", padx=4)
        ttk.Button(bar, text="关闭", command=top.destroy, width=8).pack(side="right", padx=4)

    def share_import(self):
        """粘贴分享码，导入成一条编队。"""
        code = simpledialog.askstring("导入分享码", "把分享码粘贴到这里：", parent=self)
        if not code:
            return
        try:
            data = self._share_decode(code)
            team, steps = self._share_to_steps(data)
        except Exception as e:
            messagebox.showerror("导入失败", f"分享码无效：{e}")
            return
        if not steps:
            messagebox.showwarning("导入失败", "这个分享码里没有技能序列")
            return

        name = simpledialog.askstring("保存编队", "给导入的编队起个名字：", parent=self)
        if name is None:
            return
        item = {
            "name": name.strip() or "导入的轴",
            "team": team,
            "steps": steps,
            "holdMs": int(data.get("h", self.cfg.get("holdMs", 280))),
        }
        self.teams.append(item)
        self.cfg["teams"] = self.teams
        try:
            save_cfg(self.cfg)
        except Exception:
            pass
        self.refresh_tlib()
        self.status.config(text=f"已导入编队：{item['name']}（{len(steps)} 步）", fg="#3ddc84")

    def open_team_panel(self):
        """右侧快捷编队侧栏。"""
        if getattr(self, "_team_panel", None):
            try:
                self._team_panel.destroy()
            except Exception:
                pass
            self._team_panel = None
            return

        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg="#141922")
        self._team_panel = top
        self._team_next = 0          # 下一个要填的位号（0~3）

        head = tk.Frame(top, bg="#141922")
        head.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(head, text="快捷编队", bg="#141922", fg=FG,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self._team_hint = tk.Label(head, text="请点第 1 位", bg="#141922", fg="#7f8ba0",
                                   font=("Microsoft YaHei UI", 8))
        self._team_hint.pack(side="right")

        wrap = tk.Frame(top, bg="#10141b")
        wrap.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        cv = tk.Canvas(wrap, bg="#10141b", highlightthickness=0)
        cv.pack(side="left", fill="both", expand=True)
        grid = tk.Frame(cv, bg="#10141b")
        cv.create_window((0, 0), window=grid, anchor="nw")

        def _sync(_=None):
            cv.update_idletasks()
            cv.itemconfigure(1, width=cv.winfo_width())
            cv.configure(scrollregion=(0, 0, cv.winfo_width(), grid.winfo_reqheight()))

        grid.bind("<Configure>", _sync)
        cv.bind("<Configure>", _sync)
        cv.bind_all("<MouseWheel>",
                    lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
                    if getattr(self, "_team_panel", None) is top else None)

        names = [n for n in OPS if n]
        cols = 4
        for idx, nm in enumerate(names):
            r, c = divmod(idx, cols)
            cell = tk.Frame(grid, bg="#10141b", cursor="hand2")
            cell.grid(row=r, column=c, padx=3, pady=3)
            ph = self.op_card_img(nm, 76, 99)
            pic = tk.Label(cell, bg="#10141b")
            if ph:
                pic.config(image=ph)
                pic.image = ph
            pic.pack()
            for w in (cell, pic):
                w.bind("<Button-1>", lambda e, nm=nm: self.team_pick(nm))

        # 点别处自动收起
        def _maybe_close(_=None):
            self.after(120, self._check_team_panel)

        top.bind("<FocusOut>", _maybe_close)
        cv.bind("<FocusOut>", _maybe_close)

        try:
            self.update_idletasks()
            b = self.team_btn
            x = b.winfo_rootx() + b.winfo_width() + 2
            y = b.winfo_rooty()
            h = max(300, self.winfo_rooty() + self.winfo_height() - y - 4)
            top.geometry(f"366x{h}+{x}+{y}")
        except Exception:
            top.geometry("366x620+200+120")

    def clear_team(self):
        """清空四个位。"""
        for i in range(4):
            self.tvars[i].set("")
            self.relabel(i)
        self._team_next = 0
        try:
            self._team_hint.config(text="下一个空位：1 号")
        except Exception:
            pass
        self.status.config(text="已清空编队", fg="#ffa94d")

    def team_pick(self, name):
        """点一次占位，再点同一个则取消。"""
        cur = [(v.get() or "").strip() for v in self.tvars]

        # 已在队里 → 取消
        if name in cur:
            i = cur.index(name)
            self.tvars[i].set("")
            self.relabel(i)
            self._team_next = i
        else:
            # 填第一个空位
            i = cur.index("") if "" in cur else 0
            self.tvars[i].set(name)
            self.relabel(i)
            self._team_next = min(3, i + 1)

        try:
            cur2 = [(v.get() or "").strip() for v in self.tvars]
            if "" in cur2:
                self._team_hint.config(text=f"下一个空位：{cur2.index('') + 1} 号")
            else:
                self._team_hint.config(text="四位已满 ✓")
        except Exception:
            pass

    def _check_team_panel(self):
        p = getattr(self, "_team_panel", None)
        if not p:
            return
        try:
            f = self.focus_get()
            inside = f is not None and str(f).startswith(str(p))
        except Exception:
            inside = False
        if not inside:
            self.close_team_panel()

    def close_team_panel(self):
        p = getattr(self, "_team_panel", None)
        if p:
            try:
                p.destroy()
            except Exception:
                pass
            self._team_panel = None

    def sync_colors(self):
        """1/2/3/4 号标签跟着干员属性色走。"""
        gray = ELEMENT_COLORS.get("物理", (74, 78, 87))
        for i in range(4):
            name = ""
            try:
                name = (self.tvars[i].get() or "").strip()
            except Exception:
                pass
            elem = ""
            if not name:
                hexc = FG          # 没选干员：白色
            else:
                ep = op_file(name, "element.txt")
                if ep:
                    try:
                        elem = io.open(ep, encoding="utf-8").read().strip()
                    except Exception:
                        elem = ""
                r, g, b = op_color(name, gray)
                hexc = "#%02x%02x%02x" % (r, g, b)
                if elem == "物理":
                    hexc = FG      # 物理系文字用白色，灰底看不清
            cls = ""
            if name:
                cp = op_file(name, "class.txt")
                if cp:
                    try:
                        cls = io.open(cp, encoding="utf-8").read().strip()
                    except Exception:
                        cls = ""
            ph_e = self._elem_photo(elem)
            ph_c = self._class_photo(cls)
            try:
                if i < len(self.slot_labels):
                    if ph_e or ph_c:
                        self.slot_labels[i].config(text="")
                    else:
                        self.slot_labels[i].config(text=f"{i + 1} 号", fg=hexc)
            except Exception:
                pass
            try:
                if i < len(self.slot_icons):
                    self.slot_icons[i].config(image=ph_e or "")
                    self.slot_icons[i].image = ph_e
                if i < len(self.class_icons):
                    self.class_icons[i].config(image=ph_c or "")
                    self.class_icons[i].image = ph_c
            except Exception:
                pass
            # 动作区只改颜色，不动布局
            try:
                if i < len(self.act_labels):
                    self.act_labels[i].config(fg=hexc)
            except Exception:
                pass

    def rename_all(self):
        for i in range(4):
            self.relabel(i)
        self.status.config(text="已按配队重命名 ✓", fg="#3ddc84")

    def _sel(self):
        return self.sel

    def move(self, d):
        self._snap()
        i = self._sel()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self.sel = j
            self.refresh()

    def remove(self):
        self._snap()
        i = self._sel()
        if i is not None:
            self.steps.pop(i)
            self.sel = None
            self.refresh()

    def clear(self):
        self._snap()
        self.steps.clear()
        self.sel = None
        self.refresh()

    def collect(self):
        self.cfg["holdMs"] = max(80, int(float(self.v_hold.get() or 280)))
        self.cfg["opacity"] = min(1.0, max(0.3, float(self.v_alpha.get() or 0.92)))
        self.cfg["steps"] = self.steps
        self.cfg["team"] = [v.get() for v in self.tvars]
        self.cfg["teamInit"] = True
        self.cfg["freshInit"] = True
        self.cfg["teams"] = self.teams
        return self.cfg

    def save(self):
        try:
            save_cfg(self.collect())
            self.status.config(text="已保存 ✓", fg="#3ddc84")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---------------- 运行 ----------------
    def build_segments(self):
        """按 divider 把 steps 切成若干段：[(名字, [步骤...]), ...]"""
        segs = []
        cur = []
        idx = 0
        for s in self.steps:
            if s.get("divider"):
                segs.append((s.get("name") or f"第 {len(segs) + 1} 段", cur))
                cur = []
            else:
                cur.append(s)
        segs.append(("最后一段", cur))
        # 去掉空段
        segs = [(n, c) for n, c in segs if c]
        return segs or [("默认", list(self.steps))]

    def start(self):
        if not any(not s.get("divider") for s in self.steps):
            messagebox.showwarning("空序列", "先添加动作～")
            return
        self.save()
        self.segments = self.build_segments()
        self.seg = 0
        self.idx = 0
        self.press.clear()
        self._render_cards()
        try:
            self.hint.pack_forget()
            self.debug.pack_forget()
        except Exception:
            pass
        # 编辑窗口收进任务栏，运行窗口独立显示
        try:
            self.iconify()
        except Exception:
            pass
        try:
            self.run.pack(fill="both", expand=True)
            self.attributes("-alpha", 1.0)
            self.run_win.attributes("-alpha", float(self.cfg.get("opacity", 0.92)))
            self.run_win.attributes("-topmost", True)
            self.run_win.overrideredirect(True)
            self.run_win.deiconify()
            self.run_win.lift()
            self.update_idletasks()
            w = self.strip.winfo_reqwidth() + 20
            h = self.strip.winfo_reqheight() + 10
            sw = self.run_win.winfo_screenwidth()
            sh = self.run_win.winfo_screenheight()
            px, py = self.winfo_x(), self.winfo_y()
            if px < 0 or py < 0:
                px, py = 80, 300
            # 不让窗口超出屏幕：超了就左移 / 上移
            if px + w > sw:
                px = max(0, sw - w - 8)
            if py + h > sh:
                py = max(0, sh - h - 40)
            self.run_win.geometry(f"{w}x{h}+{px}+{py}")
        except Exception as e:
            log(f"run window fail {e}")
        try:
            _ime_save_current()
        except Exception:
            pass
        _ime_watch()
        self.running = True
        log(f"start run segments={len(self.segments)}")

    def stop(self, minimize=False):
        """minimize=True：轴自然结束，只收起运行窗口（编辑窗口本来就在最小化）。"""
        self.running = False
        try:
            _ime_restore_last()
        except Exception:
            pass
        try:
            self.run.pack_forget()
            self.run_win.withdraw()
            self.run_win.attributes("-topmost", False)
        except Exception:
            pass
        try:
            self.hint.pack(pady=(0, 4))
            self.debug.pack(pady=(0, 6))
        except Exception:
            pass
        try:
            self.edit.pack(fill="both", expand=True)
        except Exception:
            pass
        if not minimize:
            # 手动返回：把编辑窗口还原出来（尺寸也复位）
            try:
                self.state("normal")
                self.deiconify()
                self.geometry("1080x640+100+60")
                self.lift()
                self.focus_force()
            except Exception:
                pass

    def cur_steps(self):
        if not hasattr(self, "segments") or not self.segments:
            self.segments = self.build_segments()
        self.seg = min(self.seg, len(self.segments) - 1)
        return self.segments[self.seg][1]

    def _thumb(self, path, dim=False, size=52):
        key = (path, dim, size)
        if key in self._thumb_cache:
            return self._thumb_cache[key]
        try:
            im = Image.open(path).convert("RGBA")
            im.thumbnail((size, size), Image.LANCZOS)
            if dim:
                a = im.split()[3].point(lambda v: int(v * 0.28))
                im.putalpha(a)
            ph = ImageTk.PhotoImage(im)
        except Exception:
            ph = None
        self._thumb_cache[key] = ph
        return ph

    def _render_cards(self):
        for w in self.strip.winfo_children():
            w.destroy()
        self.cards = []
        steps = self.cur_steps()
        name = self.segments[self.seg][0] if len(self.segments) > 1 else ""

        if name:
            lab = tk.Label(self.strip, text=name, bg=BG_RUN, fg=SUB,
                           font=("Microsoft YaHei UI", 9, "bold"))
            lab.pack(side="left", padx=(0, 6))
            lab.bind("<Button-1>", self._drag_start)
            lab.bind("<B1-Motion>", self._drag_move)

        for i, s in enumerate(steps):
            k = str(s.get("key", "?")).upper()
            kind = "ult" if s.get("hold") else ("link" if k == "E" else "skill")
            oi = int(s.get("op", 0) or 0)
            team = self.cfg.get("team", ["", "", "", ""])
            who = team[oi - 1] if 0 < oi <= len(team) else ""
            col = op_color(who, ELEMENT_COLORS.get("物理", (74, 78, 87)), kind)
            path = icon(kind, col, str(oi or ""), who)

            cell = tk.Frame(self.strip, bg=BG_RUN)
            cell.pack(side="left", padx=4)
            note = tk.Label(cell, text=(s.get("note") or ""), bg=BG_RUN,
                            fg=BG_RUN, font=("Microsoft YaHei UI", 9, "bold"))
            note.pack(pady=(0, 1))
            lb = tk.Label(cell, bg=BG_RUN, bd=0)
            ph = self._thumb(path, dim=False)
            if ph:
                lb.config(image=ph)
                lb.image = ph
            lb.pack()
            if kind == "link":
                kind_txt = "连携技"
            elif kind == "ult":
                kind_txt = f"{oi}终结技"
            else:
                kind_txt = f"{oi}战技"
            tag = tk.Label(cell, text=kind_txt, bg=BG_RUN, fg=SUB,
                           font=("Microsoft YaHei UI", 9))
            tag.pack(pady=(2, 0))
            for w in (cell, lb, tag):
                w.bind("<Button-1>", self._drag_start)
                w.bind("<B1-Motion>", self._drag_move)
            self.cards.append({"cell": cell, "pic": lb, "tag": tag,
                               "note": note, "col": col, "path": path})
        self._paint()

    def _paint(self):
        """当前步：图标原色 + 文字属性色；其余：图标压暗 + 文字灰。"""
        for i, c in enumerate(self.cards):
            cur = (i == self.idx)
            ph = self._thumb(c["path"], dim=not cur)
            if ph:
                c["pic"].config(image=ph)
                c["pic"].image = ph
            c["tag"].config(fg=("#%02x%02x%02x" % tuple(c["col"])) if cur else DIM)
            try:
                if c.get("note") is not None:
                    c["note"].config(fg=("#%02x%02x%02x" % tuple(c["col"])) if cur else DIM)
            except Exception:
                pass

    def next_segment(self):
        """当前段走完：有下一段就切过去；全部跑完就回到编辑界面。"""
        if self.seg + 1 >= len(self.segments):
            log("axis finished -> back to editor (minimized)")
            self.stop(minimize=True)
            return
        self.seg += 1
        self.idx = 0
        self._render_cards()
        try:
            self.update_idletasks()
            w = self.strip.winfo_reqwidth() + 20
            h = self.strip.winfo_reqheight() + 10
            sw = self.run_win.winfo_screenwidth()
            sh = self.run_win.winfo_screenheight()
            px, py = self.run_win.winfo_x(), self.run_win.winfo_y()
            if px + w > sw:
                px = max(0, sw - w - 8)
            if py + h > sh:
                py = max(0, sh - h - 40)
            self.run_win.geometry(f"{w}x{h}+{px}+{py}")
        except Exception:
            pass

    # ---------------- 按键 ----------------
    def on_key(self, e):
        """钩子线程：只把事件塞进队列，绝不碰 Tk。"""
        try:
            name = (e.name or "").lower()
            if name not in ("1", "2", "3", "4", "e", "f8", "f9", "esc"):
                return True
            self.q.put((e.event_type, name))
            return True
        except Exception as ex:
            log(f"cb err {ex}")
            return True

    def _poll_keys(self):
        """直接读键盘状态：不依赖钩子，后台也有用。"""
        if self._u32 is None:
            return
        for vk, name in VK_MAP.items():
            down = bool(self._u32.GetAsyncKeyState(vk) & 0x8000)
            was = self._prev.get(vk, False)
            if down and not was:
                self._prev[vk] = True
                self._down(name, src="poll")
            elif not down and was:
                self._prev[vk] = False
                self._up(name, src="poll")

    def _pump(self):
        """主线程：每 10ms 取一次队列。"""
        try:
            self._tick = getattr(self, "_tick", 0) + 1
            if self._tick % 200 == 1:
                try:
                    if self._u32 is None:
                        probe = "u32=None"
                    else:
                        probe = " ".join(
                            f"{k}={self._u32.GetAsyncKeyState(v) & 0xFFFF}"
                            for k, v in (("1", 0x31), ("2", 0x32), ("E", 0x45)))
                except Exception as ex:
                    probe = f"probe err {ex}"
                log(f"pump alive tick={self._tick} running={self.running} {probe}")
        except Exception:
            pass
        try:
            self._poll_keys()
        except Exception as ex:
            log("poll err " + str(ex))
        try:
            while True:
                kind, name = self.q.get_nowait()
                if kind == "down":
                    self._down(name, src="hook")
                else:
                    self._up(name, src="hook")
        except queue.Empty:
            pass
        except Exception as ex:
            log(f"pump err {ex}")
        try:
            self.after(10, self._pump)
        except Exception:
            pass

    def _down(self, name, src="hook"):
        log(f"{src} down {name} running={self.running}")
        if name == "f8":
            log(f"f8 pressed running={self.running}")
            if self.running:
                try:
                    self.stop()
                except Exception as ex:
                    log(f"stop fail {ex}")
                    try:
                        self.overrideredirect(False)
                        self.run.pack_forget()
                        self.edit.pack(fill="both", expand=True)
                        self.running = False
                    except Exception as ex2:
                        log(f"fallback fail {ex2}")
            return
        if name == "f9":
            if self.running:
                # 回到第一段开头，而不是只回到当前段开头
                self.seg = 0
                self.idx = 0
                try:
                    self._render_cards()
                    self.update_idletasks()
                    w = self.strip.winfo_reqwidth() + 20
                    h = self.strip.winfo_reqheight() + 10
                    sw = self.run_win.winfo_screenwidth()
                    sh = self.run_win.winfo_screenheight()
                    px, py = self.run_win.winfo_x(), self.run_win.winfo_y()
                    if px + w > sw:
                        px = max(0, sw - w - 8)
                    if py + h > sh:
                        py = max(0, sh - h - 40)
                    self.run_win.geometry(f"{w}x{h}+{px}+{py}")
                except Exception:
                    pass
                self._paint()
                self.debug.config(text="已重置到第一段")
            return
        if not self.running:
            self.status.config(text=f"最近按键：{name.upper()}", fg="#3ddc84")
            return
        if name in self.press:
            return
        self.press[name] = time.time()
        self.debug.config(text=f"收到 {name.upper()} …")

    def _up(self, name, src="hook"):
        log(f"{src} up {name} running={self.running}")
        if not self.running or name in ("f8", "f9"):
            return
        t0 = self.press.pop(name, None)
        if t0 is None:
            return
        dur = (time.time() - t0) * 1000.0
        self.fire(name.upper(), dur)

    def fire(self, key, dur):
        steps = self.cur_steps()
        log(f"fire {key} {dur:.0f}ms seg={self.seg} idx={self.idx}/{len(steps)}")
        if self.idx >= len(steps):
            return
        cur = steps[self.idx]
        if str(cur.get("key", "")).upper() != key:
            self.debug.config(text=f"{key} ✗ 不是这一步")
            return
        if bool(cur.get("hold")) and dur < int(self.cfg.get("holdMs", 280)):
            self.debug.config(text=f"{key} 按得不够久（{dur:.0f}ms）")
            return
        self.idx += 1
        if self.idx >= len(steps):
            self.next_segment()
        else:
            self._paint()
        self.debug.config(text=f"{key} ✓")

    def quit_app(self):
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
