# -*- coding: utf-8 -*-
"""Endfield RotPilot · Web 版

思路：界面用 HTML/CSS 写，交给系统自带 Edge 以 --app 模式显示（无地址栏，
看起来就像原生应用）。Python 这边只负责：
  * 本地 HTTP 服务（提供界面和图标）
  * 读取/保存配置
  * 键盘钩子 + 置顶运行条（Tk，逻辑跟老版一致）
"""
import base64
import gzip
import io
import json
import re
import shutil
import mimetypes
import os
import queue
import sys
import threading
import time
import webbrowser
import zipfile
import zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))

# GitHub 更新
UPDATE_REPO = "HermesStar37/EndfieldRotPilot"
UPDATE_API = os.environ.get("ROTPILOT_UPDATE_API", "https://api.github.com")
UPDATE_MIRROR = os.environ.get("ROTPILOT_UPDATE_MIRROR", "")   # 例如 https://ghproxy.net/
UPDATE_DIR = os.path.join(os.environ.get("TEMP", BASE), "rotpilot_update")
_UPDATE = {"state": "idle", "received": 0, "total": 0, "path": "", "error": ""}


def _ver_tuple(v):
    """把 v1.4 / v1.4.1 / v1.4.1.2 统一成三位元组，缺的补 0。"""
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _update_get(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "EndfieldRotPilot",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def update_check():
    """读 GitHub 的 Release，挑出「版本号型」的最新一个，和本地比。"""
    base = UPDATE_API.rstrip("/")
    data = None
    try:
        # 先拿列表：里面可能有「新干员」这种非版本号 tag，要跳过
        items = _update_get("%s/repos/%s/releases?per_page=30" % (base, UPDATE_REPO))
        best = None
        best_tuple = ()
        for it in items if isinstance(items, list) else []:
            tag = str(it.get("tag_name") or "")
            if not re.search(r"\d", tag):
                continue
            vt = _ver_tuple(tag)
            if best is None or vt > best_tuple:
                best, best_tuple = it, vt
        if best is None:
            data = _update_get("%s/repos/%s/releases/latest" % (base, UPDATE_REPO))
        else:
            data = best
    except Exception as e:
        try:
            data = _update_get("%s/repos/%s/releases/latest" % (base, UPDATE_REPO))
        except Exception as e2:
            return {"ok": False, "error": "连不上 GitHub：%s" % e2, "current": APP_VERSION}

    latest = str(data.get("tag_name") or "").strip()
    assets = data.get("assets") or []
    url = ""
    size = 0
    for a in assets:
        n = str(a.get("name") or "")
        if n.lower().endswith(".zip") and not n.lower().startswith("operators"):
            url = a.get("browser_download_url") or ""
            size = int(a.get("size") or 0)
            break
    if not url:
        for a in assets:
            if str(a.get("name") or "").lower().endswith(".zip"):
                url = a.get("browser_download_url") or ""
                size = int(a.get("size") or 0)
                break
    if UPDATE_MIRROR and url:
        url = UPDATE_MIRROR.rstrip("/") + "/" + url
    return {"ok": True, "current": APP_VERSION, "latest": latest or APP_VERSION,
            "newer": bool(latest) and _ver_tuple(latest) > _ver_tuple(APP_VERSION),
            "notes": (data.get("body") or "")[:6000], "url": url, "size": size,
            "page": data.get("html_url") or ""}


def import_operators_pack(data, filename=""):
    """导入一个「干员包」（里面可以有多名干员，每个目录一份 operator.json）。"""
    import tempfile
    import zipfile
    if not data:
        return False, {}, "空文件"
    tmpdir = tempfile.mkdtemp(prefix="oppack_")
    zpath = os.path.join(tmpdir, "pack.zip")
    with open(zpath, "wb") as f:
        f.write(data)
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmpdir)
    except Exception as e:
        return False, {}, "压缩包打不开：%s" % e

    roots = []
    for base, _dirs, files in os.walk(tmpdir):
        if "operator.json" in files:
            roots.append(base)
    if not roots:
        return False, {}, "包里没有 operator.json"

    names, errors = [], []
    for root in roots:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in os.listdir(root):
                p = os.path.join(root, fn)
                if os.path.isfile(p):
                    z.write(p, fn)
        ok, info = import_operator_zip(buf.getvalue(), filename)
        if ok:
            names.append(info.get("name") if isinstance(info, dict) else str(info))
        else:
            errors.append("%s: %s" % (os.path.basename(root), info if not isinstance(info, dict) else info.get("error")))
    if not names:
        return False, {}, "；".join(errors[:3]) or "全部导入失败"
    return True, {"count": len(names), "names": names, "errors": errors[:3]}, ""


def update_ops_check():
    """看最新 Release 里有没有干员包（operators_*.zip）。"""
    try:
        data = _update_get("%s/repos/%s/releases/latest" % (UPDATE_API.rstrip("/"), UPDATE_REPO))
    except Exception as e:
        return {"ok": False, "error": "连不上 GitHub：%s" % e}
    asset = None
    for a in (data.get("assets") or []):
        n = str(a.get("name") or "")
        if n.lower().startswith("operators") and n.lower().endswith(".zip"):
            asset = a
            break
    if not asset:
        return {"ok": True, "found": False, "latest": data.get("tag_name") or ""}
    cfg = load_cfg()
    local = (cfg.get("opPack") or {}).get("name") or ""
    return {"ok": True, "found": True,
            "name": asset.get("name"), "size": int(asset.get("size") or 0),
            "url": asset.get("browser_download_url"),
            "published": asset.get("updated_at") or "",
            "installed": local, "newer": bool(local) is False or local != asset.get("name")}

def update_download(url):
    """后台下载更新包，进度写进 _UPDATE。"""
    import urllib.request
    _UPDATE.update({"state": "downloading", "received": 0, "total": 0, "path": "", "error": ""})
    os.makedirs(UPDATE_DIR, exist_ok=True)
    path = os.path.join(UPDATE_DIR, "update.zip")
    log("update download: dir=%s url=%s" % (UPDATE_DIR, url[:120]))

    def run():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "EndfieldRotPilot"})
            with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                _UPDATE["total"] = total
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    _UPDATE["received"] += len(chunk)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            log("update download done: %s (%d bytes)" % (path, size))
            if size < 1024:
                _UPDATE.update({"state": "error", "error": "下到的文件只有 %d 字节" % size})
                return
            _UPDATE.update({"state": "done", "path": path})
        except Exception as e:
            _UPDATE.update({"state": "error", "error": str(e)})
            log("update download fail: %s" % e)

    threading.Thread(target=run, daemon=True).start()


def update_apply():
    """写 bat：解压覆盖 → 必要时把外层版本文件夹改名 → 重启。"""
    zip_path = _UPDATE.get("path") or ""
    if not zip_path or not os.path.exists(zip_path):
        cand = os.path.join(UPDATE_DIR, "update.zip")
        if os.path.exists(cand) and os.path.getsize(cand) > 1024:
            zip_path = cand
    if not zip_path or not os.path.exists(zip_path):
        log("update apply: no zip")
        return False, "没有找到更新包：%s" % os.path.join(UPDATE_DIR, "update.zip")

    # 从更新包里读出版本号
    new_ver = ""
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.endswith("app_web.py"):
                    txt = z.read(n).decode("utf-8", "ignore")
                    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', txt)
                    if m:
                        new_ver = m.group(1)
                    break
    except Exception as e:
        log("update apply: read version fail %s" % e)

    base = BASE.rstrip("\\/")
    parent = os.path.dirname(base)
    old_name = os.path.basename(base)
    vz = r"^(.*?)[_-]v?\d+(?:\.\d+)*(_test)?$"

    def _new_name(name):
        m = re.match(vz, name)
        if not (new_ver and m):
            return None
        return "%s_%s" % (m.group(1), new_ver)

    new_base = base
    move_src = ""
    # 情况 1：程序目录本身带版本号
    nn = _new_name(old_name)
    if nn:
        cand = os.path.join(parent, nn)
        if not os.path.exists(cand):
            new_base, move_src = cand, base
    # 情况 2：外面那层文件夹带版本号（常见：EndfieldRotPilot_v1.3.9_test\EndfieldRotPilot）
    if not move_src:
        pname = os.path.basename(parent)
        pn = _new_name(pname)
        if pn:
            new_parent = os.path.join(os.path.dirname(parent), pn)
            if not os.path.exists(new_parent):
                move_src = parent
                new_base = os.path.join(new_parent, old_name)
    log("update apply: version=%s dir %r -> %r (move %r)" % (new_ver, base, new_base, move_src))

    dst = os.path.join(os.environ.get("TEMP", BASE), "rotpilot_new")
    bat = os.path.join(os.environ.get("TEMP", BASE), "rotpilot_update.bat")
    inner = os.path.join(dst, "EndfieldRotPilot")
    cfg = os.path.join(base, "config.json")
    cfgbak = os.path.join(base, "config.json.bak")
    L = [
        "@echo off",
        "chcp 65001 >nul",
        'set LOG=%TEMP%\\rotpilot_update.log',
        'cd /d "%TEMP%"',
        'echo ==== start %date% %time% ==== >> "%LOG%"',
        "timeout /t 2 /nobreak >nul",
        'if exist "' + dst + '" rmdir /s /q "' + dst + '"',
        'mkdir "' + dst + '"',
        'tar -xf "' + zip_path + '" -C "' + dst + '" >> "%LOG%" 2>&1',
        'echo extract rc=%errorlevel% >> "%LOG%"',
        'copy /y "' + cfg + '" "' + cfgbak + '" >nul 2>nul',
        'robocopy "' + inner + '" "' + base + '" /E /XF config.json /NFL /NDL /NJH /NJS >> "%LOG%" 2>&1',
        'echo copy rc=%errorlevel% >> "%LOG%"',
    ]
    if new_base != base and move_src:
        move_target = new_base if move_src == base else os.path.dirname(new_base)
        L += [
            'if exist "' + move_target + '" goto skipmove',
            'move "' + move_src + '" "' + move_target + '" >> "%LOG%" 2>&1',
            'echo move rc=%errorlevel% >> "%LOG%"',
            ":skipmove",
        ]
    L += [
        'start "" "' + new_base + '\\runtime\\pythonw.exe" "' + new_base + '\\app_web.py"',
        'echo start rc=%errorlevel% >> "%LOG%"',
        'del "%~f0"',
    ]
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(L) + "\r\n")
    log("update apply: bat=%s zip=%s" % (bat, zip_path))
    try:
        close_app_window()
    except Exception as e:
        log("close window fail %s" % e)
    import subprocess
    subprocess.Popen(["cmd", "/c", bat], cwd=os.environ.get("TEMP", BASE),
                     creationflags=0x08000000)
    return True, bat
# 版本信息（设置里「关于」显示用）
APP_VERSION = "v1.4"
APP_BUILD = "2026-10-26"


# 素材与图标函数：优先用本目录下的 core（自带，独立运行）
CORE_DIR = os.path.join(BASE, "core")     # 自带，全部东西都在本目录
sys.path.insert(0, CORE_DIR)

# ---------- 干员名订正：官方名是“诀”，作者数据里写成了“决” ----------
# 这里在加载 core 之前把目录/图片/名单改过来，之后所有地方（界面、分享码、
# 运行条）都会统一显示成“诀（意）/诀（智）”。
# 幂等：已经是“诀”就什么都不做；作者以后自己改对了也不会出问题。
NAME_FIX = {"决（意）": "诀（意）", "决（智）": "诀（智）"}


def _map_name(s):
    for a, b in NAME_FIX.items():
        if a in s:
            s = s.replace(a, b)
    return s


def _fix_names_core():
    root = CORE_DIR
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for nm in list(filenames) + list(dirnames):
            new = _map_name(nm)
            if new == nm:
                continue
            src, dst = os.path.join(dirpath, nm), os.path.join(dirpath, new)
            if os.path.exists(dst):
                continue
            try:
                os.rename(src, dst)
            except Exception:
                pass
    rp = os.path.join(root, "operators", "roster.json")
    try:
        if os.path.isfile(rp):
            with open(rp, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            changed = False
            if isinstance(data, dict):
                for k, v in list(data.items()):
                    if isinstance(v, str) and _map_name(v) != v:
                        data[k] = _map_name(v)
                        changed = True
            if changed:
                with open(rp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # 卡面是把名字直接画进图片的，只改文件名不够：这里重画一次（只做一次）
    marker = os.path.join(root, ".names_fixed_cards")
    if not os.path.exists(marker):
        done = False
        try:
            import make_cards
            for new_name in NAME_FIX.values():
                if not os.path.isdir(os.path.join(root, "operators", new_name)):
                    continue
                try:
                    make_cards.make_card(new_name).save(
                        os.path.join(root, "cards", new_name + ".png"))
                    done = True
                except Exception:
                    pass
        except Exception:
            pass
        if done:
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception:
                pass


_fix_names_core()

import app as core  # noqa: E402  （老版里的数据与图标函数）

PORT = 8777
BACKUP_DIR = os.path.join(BASE, "_backup")


def _writable(path):
    """目录能不能写（某些沙箱环境下会退到别处）。"""
    try:
        d = os.path.dirname(path) or "."
        test = os.path.join(d, "._wtest")
        with open(test, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(test)
        return True
    except Exception:
        return False


def _pick_cfg():
    cands = [
        os.environ.get("ROTPILOT_CFG"),
        os.path.join(BASE, "config.json"),
        os.path.join(os.environ.get("TEMP", BASE), "rotpilot_web_cfg.json"),
    ]
    for c in cands:
        if c and _writable(c):
            return c
    return os.path.join(BASE, "config.json")


CFG = _pick_cfg()

# ---------- 键位 ----------
DEFAULT_KEYS = {
    "skill": ["1", "2", "3", "4"],
    "ult": ["", "", "", ""],
    "link": "E",
    "exit": "F8",
    "reset": "F9",
    "alphaDown": "F6",      # 运行条变淡
    "alphaUp": "F7",        # 运行条变实
    "loopToggle": "F10",    # 运行中开/关循环
    "loopJump": "F11",      # 立刻跳回循环起点
}

# 名称 -> 虚拟键码（只收录游戏里可能用到的）
_VK = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,
    "SPACE": 0x20, "TAB": 0x09, "ENTER": 0x0D, "SHIFT": 0x10,
    "CTRL": 0x11, "ALT": 0x12, "ESC": 0x1B, "BACKSPACE": 0x08,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "M3": 0x04, "M4": 0x05, "M5": 0x06,     # 鼠标中键 / 侧键 1 / 侧键 2
}
for _i in range(1, 13):
    _VK["F%d" % _i] = 0x6F + _i


def key_to_vk(name):
    if not name:
        return None
    return _VK.get(str(name).strip().upper())


def normalize_keys(raw):
    """把配置里的键位补全成完整结构。"""
    keys = json.loads(json.dumps(DEFAULT_KEYS))
    if isinstance(raw, dict):
        for k in ("skill", "ult"):
            v = raw.get(k)
            if isinstance(v, list):
                for i in range(min(4, len(v))):
                    if isinstance(v[i], str):
                        keys[k][i] = v[i].strip()
                            # skill 为空不合法，回退默认
        for i in range(4):
            if not keys["skill"][i]:
                keys["skill"][i] = DEFAULT_KEYS["skill"][i]
        for k in ("link", "exit", "reset", "alphaDown", "alphaUp", "loopToggle", "loopJump"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                keys[k] = v.strip()
    return keys



DEFAULT_CFG = {
    "keys": DEFAULT_KEYS,
    "inputMode": "kb",
    "overlayStyle": "focus",
    "loopCount": 0,
    "loopFrom": 0,
    "loopTo": 0,
    "displayMode": "kb",
    "comps": [],
    "padType": "ds",
    "pad": {"modifier": "winmm:B5",
            "slots": ["winmm:B1", "winmm:B4", "winmm:B3", "winmm:B2"],
            "link": "winmm:B6", "exit": "", "reset": ""},
    "holdMs": 280,
    "opacity": 0.94,
    "uiScale": 1,
    "nextPreview": 0,
    "autoExit": 1,
    "demoSeeded": 1,          # 关掉主窗口后要不要连后台一起退（默认不退）
    "steps": [],
    "team": ["", "", "", ""],
    "teams": [],
    "freshInit": True,
    # 多显示器：game=跟随游戏窗口所在屏（默认） / primary=主屏 / secondary=副屏
    "screenMode": "game",
    "winMax": False,        # 上次关的时候是不是最大化（下次照样最大化）
    "overlayMonitor": "",   # 运行条上次在哪块屏
    "winRect": None,        # 上次关闭时主窗口的位置大小 {x,y,w,h}
    "winMonitor": "",       # 上次在哪块屏（设备名），换了屏就不硬套旧坐标
}


_LOG_LOCK = threading.Lock()
LOG_MAX_LINES = 200


def log(msg):
    """写运行日志；超过 200 行自动截断，只留最近的。"""
    try:
        path = os.path.join(os.path.dirname(CFG) or BASE, "web_run.log")
        line = time.strftime("%H:%M:%S ") + str(msg) + "\n"
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            # 行数超了就压缩
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > LOG_MAX_LINES:
                    with open(path, "w", encoding="utf-8") as f:
                        f.writelines(lines[-LOG_MAX_LINES:])
            except Exception:
                pass
    except Exception:
        pass


def _fix_names_cfg(cfg):
    """把配置里的旧名“决（意）/决（智）”统一改成“诀（意）/诀（智）”。
    读配置时顺手改，保存时自然落盘。"""
    if not isinstance(cfg, dict):
        return cfg

    def fix_team(t):
        if isinstance(t, list):
            return [_map_name(x) if isinstance(x, str) else x for x in t]
        return t

    cfg["team"] = fix_team(cfg.get("team"))
    for key in ("comps", "teams"):
        arr = cfg.get(key)
        if isinstance(arr, list):
            for item in arr:
                if not isinstance(item, dict):
                    continue
                item["team"] = fix_team(item.get("team"))
                nm = item.get("name")
                if isinstance(nm, str) and _map_name(nm) != nm:
                    item["name"] = _map_name(nm)
                st = item.get("steps")
                if isinstance(st, list):
                    for s in st:
                        if isinstance(s, dict):
                            for k in ("who", "label"):
                                v = s.get(k)
                                if isinstance(v, str) and _map_name(v) != v:
                                    s[k] = _map_name(v)
    steps = cfg.get("steps")
    if isinstance(steps, list):
        for s in steps:
            if not isinstance(s, dict):
                continue
            for k in ("who", "label", "note", "name"):
                v = s.get(k)
                if isinstance(v, str) and _map_name(v) != v:
                    s[k] = _map_name(v)
    return cfg


def load_cfg():
    return _fix_names_cfg(_load_cfg_raw())


def _load_cfg_raw():
    if not os.path.exists(CFG):
        # 主配置没了，先看备份
        bak = CFG + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                log("config missing, restored from .bak")
                return cfg
            except Exception:
                pass
        return dict(DEFAULT_CFG)
    try:
        with open(CFG, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:
        # 主配置读坏了（比如写到一半），退回备份，不要直接给默认值
        bak = CFG + ".bak"
        try:
            with open(bak, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            log("config broken (%s), restored from .bak" % e)
            return cfg
        except Exception:
            log("config broken (%s), no usable .bak, using defaults" % e)
            return dict(DEFAULT_CFG)
    for k, v in DEFAULT_CFG.items():
        cfg.setdefault(k, v)
    return cfg


def save_cfg(cfg):
    # 每次保存前，把上一版留一份；配置被写坏时可以回来找回
    try:
        if os.path.exists(CFG):
            shutil.copy2(CFG, CFG + ".bak")
    except Exception:
        pass
    tmp = CFG + (".%d.tmp" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG)


# ---------------- 内置示例轴 ----------------
# 为什么内置：群里天天有人问「循环怎么做」「反复怎么配」「重击要不要按」。
# 与其一遍遍解释，不如第一次打开就有一条排好的轴，注释直接写在格子上，
# 照着跑一遍就懂了。
DEMO_TEAM = ["伊冯", "汤汤", "祀", "诀（智）"]
DEMO_AXIS_A = "示例甲 · 基本操作与循环"
DEMO_AXIS_B = "示例乙 · 嵌套 / 多终点 / 无限"
DEMO_COMP = "示例编队"


def _demo_sk(key, hold=False, op=None, note=None):
    """造一个技能格（名字按示例编队自动算好）。"""
    k = str(key).upper()
    if op is None:
        op = int(k) if k in ("1", "2", "3", "4") else 1
    who = DEMO_TEAM[op - 1] if 0 < op <= len(DEMO_TEAM) else ""
    kind = "连携技" if k == "E" else ("终结技" if hold else "战技")
    s = {"key": str(key), "hold": bool(hold), "op": int(op), "who": who,
         "label": (who + " · " + kind) if who else kind}
    if note:
        s["note"] = note
    return s


def demo_axes():
    """两条示例轴：甲覆盖日常（分区/换行/注释/重击/三种技能/普通反复），
    乙只讲进阶（嵌套、一个起点两个终点、无限）。"""
    a = []
    a.append({"divider": True, "name": "① 基本操作"})
    a.append({"note": "这是内置示例。跟着条上的提示按键，条才会往前走 —— 按错了它是不动的。"})
    a.append(_demo_sk("1"))
    a.append(_demo_sk("E", op=2, note="连携技：看到 E 就按 E"))
    a.append(_demo_sk("E", op=3))
    a.append(_demo_sk("4"))
    a.append(_demo_sk("1", hold=True, op=1, note="终结技：按住 1~4 不放，别只点一下"))
    a.append({"br": True})
    a.append({"note": "上一条虚线是「换行」，只管排版，不影响跑的顺序"})
    a.append({"heavy": True, "note": "重击不用按键，条会自己跳过"})
    a.append({"divider": True, "name": "② 循环（反复）怎么做"})
    a.append({"note": "要循环哪一段，就在那段的开头和结尾各插一个「反复」："
                      "上面那个当起点、下面那个当终点，中间这段就会来回跑。"})
    a.append({"rep": 1, "link": "A", "end": False, "times": 2})
    a.append(_demo_sk("2"))
    a.append(_demo_sk("E", op=1))
    a.append(_demo_sk("3"))
    a.append({"rep": 1, "link": "A", "end": True, "times": 2})
    a.append({"note": "上面这个就是 ×2。想改 3 遍：双击那个 ×2 直接填数字，或点旁边的 ＋；"
                      "想一直循环就点 ∞（示例乙里有整段无限）。"})
    a.append({"divider": True, "name": "③ 收尾"})
    a.append(_demo_sk("2", hold=True, op=2))
    a.append(_demo_sk("3", hold=True, op=3))
    a.append({"note": "跑完这一格就结束。想从头再练一遍，按 F9。"})

    b = []
    b.append({"divider": True, "name": "① 嵌套：外层 ×2，里面再 ×3"})
    b.append({"note": "内层先跑满 3 遍，外层才走一圈；也就是这段一共会跑 2×3 = 6 遍。"})
    b.append({"rep": 1, "link": "OUT", "end": False, "times": 2})
    b.append(_demo_sk("1"))
    b.append({"rep": 1, "link": "IN", "end": False, "times": 3})
    b.append(_demo_sk("E", op=2))
    b.append(_demo_sk("3"))
    b.append({"rep": 1, "link": "IN", "end": True, "times": 3})
    b.append(_demo_sk("4"))
    b.append({"rep": 1, "link": "OUT", "end": True, "times": 2})
    b.append({"divider": True, "name": "② 一个起点配两个终点"})
    b.append({"note": "起点只放一个、终点放两个：先在近的小圈里来回，小圈跑够了再走远的大圈。"})
    b.append({"rep": 1, "link": "M", "end": False, "times": 2})
    b.append(_demo_sk("2"))
    b.append(_demo_sk("E", op=3))
    b.append({"rep": 1, "link": "M", "end": True, "times": 2})
    b.append(_demo_sk("4"))
    b.append({"rep": 1, "link": "M", "end": True, "times": 2})
    b.append({"note": "近的终点让 2 步的小圈跑两遍，远的终点才是大圈的边界。"})
    b.append({"divider": True, "name": "③ 无限循环（按 F8 才停）"})
    b.append({"note": "最后这段会一直反复，练习用；要停就按 F8，或者点「停止运行」。"})
    b.append({"rep": 1, "link": "INF", "end": False, "times": -1})
    b.append(_demo_sk("1"))
    b.append(_demo_sk("E", op=1))
    b.append({"rep": 1, "link": "INF", "end": True, "times": -1})

    return [
        {"name": DEMO_AXIS_A, "team": list(DEMO_TEAM), "steps": a, "holdMs": 280},
        {"name": DEMO_AXIS_B, "team": list(DEMO_TEAM), "steps": b, "holdMs": 280},
    ]


# ---------------- 分享码（与老版同格式，可互通） ----------------
def share_encode(cfg, ver=2, slim=False, pack=False, fmt=None):
    """生成分享码。现在只出两种：

      rp2 = 兼容版（默认）。前缀 RP2:，内容不压缩，注释和轴名都在。
            作者原版和所有老版本都能导入 —— 只是老版本看不到轴名。
      rp3 = 压缩版。前缀 RP3:，整段 zlib 压一遍，内容一模一样、码短一截。
            只有新版（1.3.3 / 1.3.4）能读；老版本读到会报“版本不支持”
            （不会像以前那样崩在 bad base85）。

    为什么压缩版也用 RP3: 而不会和“作者原版的 v3 格式”打架：
      压缩数据解出来第一个字节固定是 0x78（zlib 的头），
      没压缩的数据第一个字节只会是版本号 2 或 3 —— 靠这个自动区分。
      所以作者原版发的 RP3 码（未压缩）我们照样能读。

    旧的 ver/slim/pack 参数还留着，前端没更新时照样能调（用来生成旧格式码）。
    """
    if fmt:
        f = str(fmt).strip().lower()
        if f not in ("rp2", "rp3"):
            f = "rp2"
        ver = 2
        slim = False
        pack = (f == "rp3")
    else:
        try:
            ver = 3 if int(ver) >= 3 else 2
        except Exception:
            ver = 2
    steps = cfg.get("steps") or []
    if slim:
        # 精简：纯注释格整格不要（计数也要跟着少，否则对方会读越界）
        steps = [s for s in steps
                 if not (s.get("note") and not s.get("key") and not s.get("heavy")
                         and not s.get("divider") and not s.get("br") and not s.get("rep"))]
    team = cfg.get("team") or ["", "", "", ""]
    buf = bytearray()
    axis_nm = (cfg.get("axisName") or "").strip()[:40]
    buf.append(ver)
    names = [n for n in core.OPS if n]
    for i in range(4):
        nm = team[i] if i < len(team) else ""
        try:
            buf.append(names.index(nm) + 1 if nm else 0)
        except ValueError:
            buf.append(0)
    import struct
    buf += struct.pack("<HH", int(cfg.get("holdMs", 280)) & 0xFFFF, len(steps) & 0xFFFF)
    texts = []
    for s in steps:
        if s.get("rep"):
            meta = "\x01rep:%s:%d:%d" % (s.get("link") or "",
                                         1 if s.get("end") else 0,
                                         _rep_times(s.get("times")))
            buf.append((5 << 5) | (1 << 1))
            texts.append(meta)
            continue
        # 纯注释格（只有一句话、不是技能）：借分段的位置放，名字加 \x01note: 前缀。
        # 以前这里会掉到下面被当成“1 号位战技”，对方导入后凭空多出几个技能。
        # 老版本读到会当成“分段名”，只影响显示、不影响跑；新版本认得出来。
        if (s.get("note") and not s.get("key") and not s.get("heavy")
                and not s.get("divider") and not s.get("br")):
            buf.append((5 << 5) | (1 << 1))
            texts.append("\x01note:" + str(s.get("note"))[:200])
            continue
        if s.get("br"):
            buf.append(6 << 5)
            continue
        if s.get("heavy"):
            # 重击也可能挂一句注释（“这个不用按”），别把它丢了
            nt = "" if slim else (s.get("note") or "")
            buf.append((7 << 5) | ((1 if nt else 0) << 1))
            if nt:
                texts.append(nt)
            continue
        if s.get("divider"):
            nm = s.get("name") or ""
            buf.append((5 << 5) | ((1 if nm else 0) << 1))
            if nm:
                texts.append(nm)
            continue
        k = str(s.get("key", "1")).upper()
        kind = {"1": 0, "2": 1, "3": 2, "4": 3, "E": 4}.get(k, 0)
        hold = 1 if s.get("hold") else 0
        op = max(0, min(3, int(s.get("op", 1) or 1) - 1))
        note = "" if slim else (s.get("note") or "")
        buf.append((kind << 5) | (hold << 4) | (op << 2) | ((1 if note else 0) << 1))
        if note:
            texts.append(note)
    # 轴名放在所有步骤文本之前（这是原版 RP3 的顺序：先名字段，再各步的注释）
    if ver >= 3:
        nb = axis_nm.encode("utf-8")
        buf += struct.pack("<H", len(nb)) + nb
    for tx in texts:
        raw = tx.encode("utf-8")
        buf += struct.pack("<H", len(raw)) + raw
    # RP2 才把名字塞在末尾：当成“多出来的一段文本”，老版本读进去但不认领、自动忽略
    if ver < 3 and axis_nm:
        raw = ("\x02name:" + axis_nm).encode("utf-8")
        buf += struct.pack("<H", len(raw)) + raw
    raw = bytes(buf)
    if pack:
        # 压缩版：前缀还是 RP3，解码时靠第一个字节 0x78 认出来
        raw = zlib.compress(raw, 9)
        return "RP3:" + base64.b85encode(raw).decode("ascii")
    return ("RP3:" if ver >= 3 else "RP2:") + base64.b85encode(raw).decode("ascii")


def rep_dup_starts(steps):
    """找出“同一组反复记号写了多个起点”的组号（后端只认第一个起点）。"""
    first, dup = {}, []
    for s in steps or []:
        if not isinstance(s, dict) or not s.get("rep") or s.get("end"):
            continue
        pid = s.get("link")
        if not pid:
            continue
        if pid in first:
            if pid not in dup:
                dup.append(pid)
        else:
            first[pid] = True
    return dup


def _pick_code(text):
    """从一大段话里把分享码捞出来。

    群里常见的是「兄弟这个轴给你 RP2:0t^cQ1... 你试试」这种，
    整段粘进来本来是报错的，这里自动只取 RP2:/RP3: 加后面那串编码字符。
    """
    t = (text or "").strip()
    if not t:
        return None
    pat = r"RP[0-9][A-Z]?:[0-9A-Za-z!#$%&()*+\-;<=>?@^_`{|}~]{16,}"
    m = re.search(pat, t, re.I)
    if m:
        return m.group(0)
    # 聊天软件复制常把长码折成几行，去掉空白再找一次
    flat = re.sub(r"\s+", "", t)
    m = re.search(pat, flat, re.I)
    return m.group(0) if m else None


def share_decode(code):
    import struct
    code = (code or "").strip()
    # 一眼就不可能是码的东西（贴了一大段乱码/整篇文档）直接劝退，别拿去解码
    if len(code) > 16384:
        raise ValueError("这串东西太长了，不像分享码（应该是 RP2: 开头的一小段）")
    # RP4: / RP2Z: / RP3Z: 是前几版用过的写法，留着照样能读
    packed = False
    if code[:4].upper() == "RP4:":
        packed = True
        code = code[4:]
    elif code[:5].upper() in ("RP2Z:", "RP3Z:"):
        packed = True
        code = code[5:]
    elif code[:4].upper() in ("RP2:", "RP3:", "RP1:"):
        code = code[4:]
    data = base64.b85decode(code.encode("ascii"))
    # 压缩过的数据第一个字节固定是 zlib 的 0x78；没压缩的第一个字节只会是版本号 2/3。
    # 靠这个自动区分「RP3 压缩版」和「作者原版的 RP3（v3 格式）」。
    if not packed and len(data) > 1 and data[0] == 0x78:
        packed = True
    if packed:
        try:
            data = zlib.decompress(data)
        except Exception:
            raise ValueError("分享码不完整或已损坏（压缩格式解不开，可能复制漏了字符）")
    # 太短 = 被截断（复制少了几位），不是“版本不支持”，别让用户一头雾水
    if len(data) < 9:
        raise ValueError("分享码不完整或已损坏（复制时可能漏了字符），请重新复制一遍")
    if data[0] not in (2, 3):
        raise ValueError("版本不支持")
    ver = data[0]
    names = [n for n in core.OPS if n]
    team = [names[data[i] - 1] if data[i] and data[i] <= len(names) else "" for i in range(1, 5)]
    hold_ms, cnt = struct.unpack("<HH", data[5:9])
    steps = []
    pos = 9
    waits = []
    for _ in range(cnt):
        b = data[pos]
        pos += 1
        kind = (b >> 5) & 0x7
        hold = (b >> 4) & 0x1
        op = ((b >> 2) & 0x3) + 1
        flag = (b >> 1) & 0x1
        if kind == 6:
            steps.append({"br": True})
            continue
        if kind == 7:
            steps.append({"heavy": True})
            waits.append((steps[-1], flag))     # 重击也可能带一句注释
            continue
        if kind == 5:
            steps.append({"divider": True, "name": ""})
        else:
            steps.append({"key": "1234E"[kind], "hold": bool(hold), "op": op})
        waits.append((steps[-1], flag))
    axis_name = ""
    if ver >= 3 and pos + 2 <= len(data):
        nl = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        try:
            axis_name = data[pos:pos + nl].decode("utf-8")
        except Exception:
            axis_name = ""
        pos += nl
    texts = []
    while pos + 2 <= len(data):
        ln = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        try:
            texts.append(data[pos:pos + ln].decode("utf-8"))
        except Exception:
            texts.append("")
        pos += ln
    # 新版把轴名字放在“最后多出来的一段文本”里（前缀 \x02name:）；老版本不认领、直接忽略
    for i, tx in enumerate(texts):
        if isinstance(tx, str) and tx.startswith("\x02name:"):
            axis_name = tx[len("\x02name:"):]
            texts.pop(i)
            break
    ti = 0
    for st, flag in waits:
        if flag and ti < len(texts):
            if st.get("divider"):
                st["name"] = texts[ti]
            else:
                st["note"] = texts[ti]
            ti += 1
    # 反复记号：解码时从「带名字的分段」里还原
    fixed = []
    for st in steps:
        nm = st.get("name") if st.get("divider") else ""
        # 纯注释格也是借分段的位置放的（前缀 \x01note:），一起还原
        if isinstance(nm, str) and nm.startswith("\x01note:"):
            fixed.append({"note": nm[len("\x01note:"):]})
            continue
        if isinstance(nm, str) and nm.startswith("\x01rep:"):
            try:
                _tag, pid, endflag, times = nm.split(":", 3)
                st = {"rep": 1, "link": pid, "end": endflag == "1",
                      "times": _rep_times(times)}
            except Exception:
                st = {"rep": 1, "link": "", "end": False, "times": 2}
        fixed.append(st)
    steps = fixed

    for st in steps:
        # 分段 / 换行 / 重击 / 反复 / 纯注释格 都没有 key，不用补名称
        if (st.get("divider") or st.get("br") or st.get("heavy") or st.get("rep")
                or not st.get("key")):
            continue
        op = st.get("op", 1)
        who = team[op - 1] if 0 < op <= 4 and team[op - 1] else f"{op} 号"
        if str(st["key"]).upper() == "E":
            st["label"] = who + " · 连携技"
        else:
            st["label"] = who + " · " + ("终结技" if st.get("hold") else "战技")
    return {"team": team, "steps": steps, "holdMs": hold_ms, "name": axis_name}


# ---------------- 图片 ----------------
def has_asset(name, kind):
    """这名干员有没有这个技能的真素材。"""
    if not name:
        return False
    p = core.op_file(name, f"{kind}.png")
    return bool(p) and os.path.exists(p)


def base_icon(name, kind, slot=1):
    """没有技能素材时：只画底层（圆环 + 属性色块 + 右下头像），不要专属符号。"""
    from PIL import Image
    col = core.op_color(name, core.ELEMENT_COLORS.get("物理", (110, 114, 120)), kind)
    if isinstance(col, str) and col.startswith("#") and len(col) == 7:
        col = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
    S = 256
    key = f"base_{kind}_{name or 'x'}.png"
    cache = os.path.join(core.GEN, key)
    os.makedirs(core.GEN, exist_ok=True)
    if os.path.exists(cache):
        return cache
    img = core._disc(tuple(col) + (255,), S, full=(kind == "ult"))
    av = core.op_file(name, "avatar.png")
    if av and os.path.exists(av):
        bd = int(S * 0.42)
        small = core._circle(Image.open(av), bd)
        img.alpha_composite(small, (S - bd - 6, S - bd - 6))
    img.resize((96, 96), Image.LANCZOS).save(cache)
    return cache


def op_icon_png(name, kind, hold=False, slot=1):
    """实时合成技能图标；缺专属素材时只画底层。"""
    if name and not has_asset(name, kind):
        return base_icon(name, kind, slot)
    col = core.op_color(name, core.ELEMENT_COLORS.get("物理", (110, 114, 120)), kind)
    # 槽位号不影响画面，缓存不按槽位分（每种技能只有一张）
    return core.icon(kind, col, "", name, badge=True)


def read_txt(path):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8-sig") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def card_path(name):
    p = os.path.join(CORE_DIR, "cards", f"{name}.png")
    return p if os.path.exists(p) else ""


def avatar_path(name):
    p = core.op_file(name, "avatar.png")
    return p or ""


# ---------------- 运行条（Tk） ----------------

# ---------- 手柄 ----------
import ctypes
from ctypes import wintypes

PAD_DEFAULT = {
    "modifier": "winmm:B5",         # 组合修饰键（长按）→ DualSense 的 L1
    "slots": ["winmm:B1", "winmm:B4", "winmm:B3", "winmm:B2"],  # □ △ ○ ×
    "link": "winmm:B6",             # R1
    "exit": "",                     # 留空 = 键盘 F8
    "reset": "",                    # 留空 = 键盘 F9
}


def normalize_pad(raw):
    pad = {"modifier": "", "slots": ["", "", "", ""],
           "link": "", "exit": "", "reset": ""}
    if isinstance(raw, dict):
        for k in ("modifier", "link", "exit", "reset"):
            v = raw.get(k)
            if isinstance(v, str):
                pad[k] = v.strip()
        sl = raw.get("slots")
        if isinstance(sl, list):
            for i in range(min(4, len(sl))):
                if isinstance(sl[i], str):
                    pad["slots"][i] = sl[i].strip()
    return pad


# ---------- 运行条透明度 ----------
ALPHA_MIN, ALPHA_MAX, ALPHA_DEFAULT = 0.10, 1.00, 0.94


def clamp_alpha(v):
    """透明度统一收口到 0.10 ~ 1.00，脏值退回默认。"""
    try:
        v = float(v)
    except Exception:
        return ALPHA_DEFAULT
    if v != v:                     # NaN
        return ALPHA_DEFAULT
    return max(ALPHA_MIN, min(ALPHA_MAX, v))


_XB_BUTTONS = [
    (0x0001, "UP"), (0x0002, "DOWN"), (0x0004, "LEFT"), (0x0008, "RIGHT"),
    (0x0010, "START"), (0x0020, "BACK"),
    (0x0040, "LS"), (0x0080, "RS"),
    (0x0100, "LB"), (0x0200, "RB"),
    (0x1000, "A"), (0x2000, "B"), (0x4000, "X"), (0x8000, "Y"),
]


def _xinput_pressed():
    """XInput（Xbox 手柄）当前按下的按键集合。"""
    import ctypes
    out = set()
    try:
        from ctypes import wintypes

        class _GP(ctypes.Structure):
            _fields_ = [("wButtons", wintypes.WORD),
                        ("bLeftTrigger", ctypes.c_ubyte),
                        ("bRightTrigger", ctypes.c_ubyte),
                        ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                        ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

        class _ST(ctypes.Structure):
            _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", _GP)]

        for dll in ("xinput1_4", "XInput1_3", "xinput9_1_0"):
            try:
                lib = ctypes.windll.LoadLibrary(dll)
            except Exception:
                continue
            for i in range(4):
                st = _ST()
                if lib.XInputGetState(i, ctypes.byref(st)) == 0:
                    b = st.Gamepad.wButtons
                    for flag, name in _XB_BUTTONS:
                        if b & flag:
                            out.add("xinput:" + name)
                    if st.Gamepad.bLeftTrigger > 40:
                        out.add("xinput:LT")
                    if st.Gamepad.bRightTrigger > 40:
                        out.add("xinput:RT")
    except Exception:
        pass
    return out


class _JOYINFOEX(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("dwXpos", wintypes.DWORD), ("dwYpos", wintypes.DWORD),
                ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD),
                ("dwButtons", wintypes.DWORD), ("dwButtonNumber", wintypes.DWORD),
                ("dwPOV", wintypes.DWORD), ("dwReserved1", wintypes.DWORD),
                ("dwReserved2", wintypes.DWORD)]


def _winmm_pressed():
    """WinMM/DirectInput 当前按下的按键（扫描 4 个槽位，设备编号变了也能读到）。"""
    out = set()
    for idx in range(4):
        try:
            j = _JOYINFOEX()
            j.dwSize = ctypes.sizeof(_JOYINFOEX)
            j.dwFlags = 0xFF
            if ctypes.windll.winmm.joyGetPosEx(idx, ctypes.byref(j)) != 0:
                continue
            b = j.dwButtons
            for i in range(32):
                if b & (1 << i):
                    out.add("winmm:B%d" % (i + 1))
            pov = j.dwPOV
            if pov in (0, 9000, 18000, 27000):
                out.add({0: "winmm:POV_UP", 9000: "winmm:POV_RIGHT",
                         18000: "winmm:POV_DOWN", 27000: "winmm:POV_LEFT"}[pov])
        except Exception:
            continue
    return out




# ---------- DualSense（PS5）Raw Input 映射 ----------
# 实测：报告 ID 0x31（蓝牙）
#   byte9  高 4 位 = □ × ○ △，低 4 位 = 十字键
#   byte10 bit0/1/2/3 = L1 / R1 / L2 / R2，bit4/5 = SHARE / OPTIONS
_DS_FACE = {0x10: "winmm:B1", 0x20: "winmm:B2", 0x40: "winmm:B3", 0x80: "winmm:B4"}
_DS_SHOULDER = {0x01: "winmm:B5", 0x02: "winmm:B6", 0x04: "winmm:B7",
                0x08: "winmm:B8", 0x10: "winmm:B9", 0x20: "winmm:B10"}

_RAW_PAD = None
_RAW_TRIED = False


def raw_pad():
    """懒启动 Raw Input 监听（需要管理员；失败就返回 None）。"""
    global _RAW_PAD, _RAW_TRIED
    if _RAW_PAD is not None:
        return _RAW_PAD
    if _RAW_TRIED:
        return None
    _RAW_TRIED = True
    try:
        import rawpad
        rp = rawpad.RawPad()
        if rp.start():
            _RAW_PAD = rp
            log("raw input started")
        else:
            log("raw input failed to start")
    except Exception as e:
        log("raw input error %s" % e)
    return _RAW_PAD


# Xbox 手柄（XInput 风格 HID 报文）的按键位表
_XB_RAW_BITS = [
    (0x0008, "START"), (0x0010, "BACK"),
    (0x0020, "A"), (0x0040, "B"), (0x0080, "X"), (0x0100, "Y"),
    (0x0200, "LB"), (0x0400, "RB"),
    (0x1000, "UP"), (0x2000, "DOWN"), (0x4000, "LEFT"), (0x8000, "RIGHT"),
]


def _xbox_raw_ids(data):
    """把 Xbox 手柄的 Raw Input 报文翻译成 xinput: 名字。"""
    out = set()
    if len(data) < 8:
        return out
    bits = data[4] | (data[5] << 8)      # 常见布局：byte4-5 是按键位域
    for mask, name in _XB_RAW_BITS:
        if bits & mask:
            out.add("xinput:" + name)
    try:
        if data[6] > 40:
            out.add("xinput:LT")
        if data[7] > 40:
            out.add("xinput:RT")
    except Exception:
        pass
    return out


def _raw_ids():
    """从 Raw Input 报告里解析出程序内部使用的手柄编号。"""
    rp = _RAW_PAD
    if rp is None:
        return set()
    out = set()
    try:
        for dev, data in rp.snapshot().items():
            if not data:
                continue
            # DualSense / PS5：报文 0x31
            if data[0] == 0x31 and len(data) >= 11:
                b9, b10 = data[9], data[10]
                for mask, name in _DS_FACE.items():
                    if b9 & mask:
                        out.add(name)
                for mask, name in _DS_SHOULDER.items():
                    if b10 & mask:
                        out.add(name)
                continue
            # Xbox 系：报文 0x01 / 0x02
            if data[0] in (0x01, 0x02):
                out |= _xbox_raw_ids(data)
    except Exception:
        pass
    return out


def pad_pressed_now():
    return _xinput_pressed() | _winmm_pressed() | _raw_ids()


def pad_devices():
    """返回检测到的手柄名称列表。"""
    import ctypes
    from ctypes import wintypes

    class JOYCAPS(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                    ("szPname", wintypes.WCHAR * 32),
                    ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT),
                    ("wYmin", wintypes.UINT), ("wYmax", wintypes.UINT),
                    ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
                    ("wNumButtons", wintypes.UINT), ("wPeriodMin", wintypes.UINT),
                    ("wPeriodMax", wintypes.UINT), ("wRmin", wintypes.UINT),
                    ("wRmax", wintypes.UINT), ("wUmin", wintypes.UINT),
                    ("wUmax", wintypes.UINT), ("wVmin", wintypes.UINT),
                    ("wVmax", wintypes.UINT), ("wCaps", wintypes.UINT),
                    ("wMaxAxes", wintypes.UINT), ("wNumAxes", wintypes.UINT),
                    ("wMaxButtons", wintypes.UINT), ("szRegKey", wintypes.WCHAR * 32),
                    ("szOEMVxD", wintypes.WCHAR * 260)]

    names = []
    try:
        for i in range(4):
            caps = JOYCAPS()
            if ctypes.windll.winmm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                names.append(caps.szPname)
    except Exception:
        pass
    try:
        import ctypes as _c
        from ctypes import wintypes as _w

        class _GP(_c.Structure):
            _fields_ = [("wButtons", _w.WORD), ("bLeftTrigger", _c.c_ubyte),
                        ("bRightTrigger", _c.c_ubyte),
                        ("sThumbLX", _c.c_short), ("sThumbLY", _c.c_short),
                        ("sThumbRX", _c.c_short), ("sThumbRY", _c.c_short)]

        class _ST(_c.Structure):
            _fields_ = [("dwPacketNumber", _w.DWORD), ("Gamepad", _GP)]

        for dll in ("xinput1_4", "XInput1_3"):
            try:
                lib = _c.windll.LoadLibrary(dll)
            except Exception:
                continue
            for i in range(4):
                st = _ST()
                if lib.XInputGetState(i, _c.byref(st)) == 0:
                    names.append("XInput 手柄 #%d" % i)
                    break
            break
    except Exception:
        pass
    return names





def pad_diag():
    """诊断用：WinMM 各槽返回码 + XInput 状态。"""
    info = {"winmm": [], "xinput": []}
    for idx in range(4):
        try:
            j = _JOYINFOEX()
            j.dwSize = ctypes.sizeof(_JOYINFOEX)
            j.dwFlags = 0xFF
            rc = ctypes.windll.winmm.joyGetPosEx(idx, ctypes.byref(j))
            info["winmm"].append({"slot": idx, "rc": rc,
                                  "buttons": j.dwButtons if rc == 0 else None})
        except Exception as e:
            info["winmm"].append({"slot": idx, "err": str(e)})
    try:
        from ctypes import wintypes as _w

        class _GP(ctypes.Structure):
            _fields_ = [("wButtons", _w.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                        ("bRightTrigger", ctypes.c_ubyte),
                        ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                        ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

        class _ST(ctypes.Structure):
            _fields_ = [("dwPacketNumber", _w.DWORD), ("Gamepad", _GP)]

        for dll in ("xinput1_4", "XInput1_3"):
            try:
                lib = ctypes.windll.LoadLibrary(dll)
            except Exception:
                continue
            for i in range(4):
                st = _ST()
                rc = lib.XInputGetState(i, ctypes.byref(st))
                info["xinput"].append({"dll": dll, "slot": i, "rc": rc,
                                       "buttons": st.Gamepad.wButtons if rc == 0 else None})
            break
    except Exception as e:
        info["xinput"].append({"err": str(e)})
    return info


_PAD_TEXT = {
    "winmm:B1": "□", "winmm:B2": "×", "winmm:B3": "○", "winmm:B4": "△",
    "winmm:B5": "L1", "winmm:B6": "R1", "winmm:B7": "L2", "winmm:B8": "R2",
    "xinput:A": "A", "xinput:B": "B", "xinput:X": "X", "xinput:Y": "Y",
    "xinput:LB": "LB", "xinput:RB": "RB", "xinput:LT": "LT", "xinput:RT": "RT",
}


def _pad_label(rid):
    return _PAD_TEXT.get(str(rid), "")


def _rep_times(v):
    """反复次数：-1 = 无限循环；其余夹到 1~999（0/非法按 2 处理，老数据兼容）。"""
    try:
        n = int(v)
    except Exception:
        return 2
    if n < 0:
        return -1
    if n == 0:
        return 2
    return max(1, min(999, n))


class Overlay:
    """置顶技能条：跟老版一样的判定，只是画得干净一点。"""

    def __init__(self, steps, team, hold_ms=280, opacity=0.94,
                 icon_size=60, pos_x=None, pos_y=150, keys=None,
                 input_mode="kb", pad=None, overlay_style="focus", loop_count=0,
                 display_mode="kb", loop_from=0, loop_to=0, screen=None,
                 ui_scale=1.0, next_preview=True):
        import tkinter as tk
        self.tk = tk
        # 摆在哪块屏：None = 按配置里的「显示屏幕」自己挑
        try:
            self.screen_idx = int(screen) if screen is not None else pick_screen()
        except Exception:
            self.screen_idx = 0
        self.auto_follow = True
        self.steps = steps
        self.team = team
        self.hold_ms = hold_ms
        self.icon_size = int(icon_size or 60)
        # 整体缩放：图标、字号、间距一起乘这个系数
        try:
            self.scale = max(0.7, min(1.5, float(ui_scale or 1)))
        except Exception:
            self.scale = 1.0
        self.icon_px = max(16, int(round(self.icon_size * self.scale)))
        self.next_preview = bool(next_preview)
        self.ghosts = []
        self.pos_x = pos_x
        self.pos_y = int(pos_y if pos_y is not None else 150)
        self.keys = normalize_keys(keys)
        self.input_mode = "pad" if str(input_mode).lower() == "pad" else "kb"
        st = str(overlay_style).lower()
        if st == "classic":
            st = "box"
        self.overlay_style = st if st in ("focus", "box", "both") else "focus"
        self.pad = normalize_pad(pad)
        try:
            self.loop_count = int(loop_count)
        except Exception:
            self.loop_count = 0
        self.passes = 0
        # 循环节点（段号，0 = 不指定）：loop_from 从哪段开始，loop_to 跑到哪段回头
        try:
            self.loop_from = max(0, int(loop_from or 0))
        except Exception:
            self.loop_from = 0
        try:
            self.loop_to = max(0, int(loop_to or 0))
        except Exception:
            self.loop_to = 0
        self.loop_on = True          # 运行中 F10 可以临时关掉，不改配置
        self.loop_count_base = self.loop_count   # 配置里的原值，F10 关了要还原
        self._alpha = clamp_alpha(opacity)
        self._tag_lb = None          # 段名标签：用来临时显示“循环 2/3”这类提示
        self._tag_text = ""
        self._tag_job = None
        self.display_mode = "pad" if str(display_mode).lower() == "pad" else "kb"
        self._imgcache = {}
        self.pad_mod = False
        self._stop_req = False
        self.pad_faces = {}          # 当前按住的面键 slot
        self.pad_link_hit = set()
        self._pad_dirty = False    # 这次按的是「当连携用」的面键
        self._pad_seen = False
        self.idx = 0
        self.press_at = {}
        self.long_fired = set()
        self.running = True

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._alpha)
        self.root.configure(bg="#0d1117")

        self.frame = tk.Frame(self.root, bg="#0d1117", padx=14, pady=12)
        self.frame.pack()

        self.cells = []
        self._dx = 0
        self._dy = 0
        self._build()
        self._place()
        self._bind_drag(self.frame)
        self.frame.bind("<Button-3>", self._menu)
        self.root.bind("<Button-3>", self._menu)
        self.frame.bind("<Button-1>", self._drag_start)
        self.frame.bind("<B1-Motion>", self._drag_move)
        self._hook()
        self._tick()
        self._keep_top()
        log("overlay started %d steps" % len(steps))

    # ---- 布局 ----
    def _seg(self):
        """切段 + 解析反复记号（一个起点可以带多个终点，可嵌套、可跨分段）。

        反复记号 {rep:1, link:组id, end:是否终点, times:N} 不是技能步骤：
        - 同一个「组id」下，第一个 end=False 的是起点，其余 end=True 的都是终点；
        - 每个「起点→终点」组成一个区间，各自带自己的次数；
        - 层数 = 被多少个别的区间严格包住（同一组多个终点时，短的自动算内层）。
        返回 ([(段名, 步骤列表)], [{id, gid, start, end, times, depth}])
        """
        packs = []
        cur, cur_name = [], ""
        starts, ends = {}, []
        gpos = 0

        for s in self.steps:
            if s.get("rep"):
                pid = s.get("link")
                if not pid:
                    continue                        # 没配对的记号：忽略
                if s.get("end"):
                    ends.append((pid, gpos - 1, _rep_times(s.get("times"))))
                else:
                    starts.setdefault(pid, gpos)    # 同组只认第一个起点
                continue
            if s.get("divider"):
                packs.append((cur_name, list(cur)))
                cur, cur_name = [], s.get("name") or ""
            elif s.get("br"):
                continue
            else:
                cur.append(s)
                gpos += 1
        packs.append((cur_name, list(cur)))
        packs = [(n, st) for n, st in packs if st]

        groups = []
        for k, (pid, epos, times) in enumerate(ends):
            spos = starts.get(pid)
            if spos is None or epos < spos:
                continue                            # 没有起点 / 空区间：忽略
            groups.append({"id": "%s#%d" % (pid, k), "gid": pid,
                           "start": spos, "end": epos, "times": times, "depth": 0})
        for g in groups:
            d = 0
            for o in groups:
                if o is g:
                    continue
                if o["start"] <= g["start"] and o["end"] >= g["end"]:
                    if o["start"] < g["start"] or o["end"] > g["end"]:
                        d += 1
            g["depth"] = d
        # 同起同止的多层（反复直接套在同一起止上）：先闭合的算更内层
        same = {}
        for g in groups:
            same.setdefault((g["start"], g["end"]), []).append(g)
        for lst in same.values():
            if len(lst) > 1:
                base, n = lst[0]["depth"], len(lst)
                for idx, g in enumerate(lst):
                    g["depth"] = base + (n - 1 - idx)
        return (packs or [("", [])]), groups

    def _build(self):
        from PIL import Image, ImageTk
        self.segments, self.rep_groups = self._seg()
        base, n = [], 0
        for _name, st in self.segments:
            base.append(n)
            n += len(st)
        self.seg_base = base
        self.total = n
        self._reset_groups()
        self.seg = 0
        self.idx = 0
        self._render()
        self._place()

    def _keep_top(self, tick=0):
        """每 2 秒强制置顶（SWP_NOACTIVATE，不抢焦点）；顺带盯着游戏有没有换屏。"""
        try:
            if not self.running:
                return
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            if tick % 2 == 0:
                try:
                    u = ctypes.windll.user32
                    hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                    HWND_TOPMOST = -1
                    SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
                    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                except Exception:
                    pass
            if tick % 3 == 0:
                self._follow_screen()
            self.root.after(2000, lambda: self._keep_top(tick + 1))
        except Exception:
            pass

    def _follow_screen(self):
        """「跟随游戏」模式下，游戏换到别的屏 → 运行条跟着过去。"""
        try:
            if not getattr(self, "auto_follow", True):
                return
            if str(load_cfg().get("screenMode") or "game").lower() != "game":
                return
            g = game_window()
            if not g["hwnd"]:
                return
            mons = enum_monitors(force=True)
            idx = _mon_of_window(g["hwnd"], mons)
            if idx < 0 or idx == getattr(self, "screen_idx", None):
                return
            log("game on screen #%d, overlay follows" % idx)
            self.set_screen(idx)
        except Exception:
            pass

    def _f(self, px):
        """字号跟着整体缩放走（最小 7，再小看不清）。"""
        return max(7, int(round(px * self.scale)))

    def _p(self, px):
        """间距跟着整体缩放走。"""
        return max(2, int(round(px * self.scale)))

    def _photo(self, src, dim=False, size=None):
        """带缓存的 PhotoImage；dim=True 返回压暗版。"""
        from PIL import Image, ImageTk, ImageEnhance
        px = int(size or self.icon_px)
        key = (src, px, bool(dim))
        if key in self._imgcache:
            return self._imgcache[key]
        im = Image.open(src).convert("RGBA")
        im.thumbnail((px, px), Image.LANCZOS)
        if dim:
            im = ImageEnhance.Brightness(im.copy()).enhance(0.34)
        # master=self.root：不这样写会用“默认根窗口”，第二次运行时会绑到已销毁的旧窗口上，
        # 结果图标一张都显示不出来（image "pyimageN" doesn't exist）
        ph = ImageTk.PhotoImage(im, master=self.root)
        self._imgcache[key] = ph
        return ph

    def _prefetch_next(self):
        """提前把下一段图标解码好，切段不卡。"""
        try:
            if self.seg + 1 >= len(self.segments):
                return
            _n, steps = self.segments[self.seg + 1]
            for s in steps:
                if s.get("br") or s.get("divider"):
                    continue
                heavy = bool(s.get("heavy"))
                kind = "heavy" if heavy else ("ult" if s.get("hold") else
                                              ("link" if str(s.get("key")).upper() == "E" else "skill"))
                op = int(s.get("op", 1) or 1)
                who = self.team[op - 1] if 0 < op <= len(self.team) else ""
                try:
                    src = (core.icon("heavy", core.ELEMENT_COLORS.get("物理", (110, 114, 120)),
                                     "", "", badge=False)
                           if heavy else op_icon_png(who, kind, s.get("hold"), op))
                    self._photo(src)
                except Exception:
                    pass
        except Exception:
            pass

    def _render(self):
        from PIL import Image, ImageTk
        for w in self.frame.winfo_children():
            w.destroy()
        self.cells = []
        self._tag_lb = None
        seg_name, steps = self.segments[self.seg] if self.segments else ("", [])
        if True:
            tag = seg_name or f"第 {self.seg + 1} 段"
            lab = self.tk.Label(self.frame, text=tag, bg="#0d1117", fg="#ffd400",
                                font=("Microsoft YaHei UI", self._f(9), "bold"))
            lab.pack(side="left", padx=(0, self._p(10)))
            self._tag_lb = lab          # 循环 / 透明度提示会临时借用这一格
            self._tag_text = tag
        for i, s in enumerate(steps):
            if s.get("br"):
                continue
            if s.get("divider"):
                lab = self.tk.Label(self.frame, text="｜" + (s.get("name") or "分段"),
                                    bg="#0d1117", fg="#7d8590",
                                    font=("Microsoft YaHei UI", self._f(8)))
                lab.pack(side="left", padx=self._p(6))
                continue
            if self._is_note_only(s):
                col = self.tk.Frame(self.frame, bg="#0d1117")
                col.pack(side="left", padx=self._p(5), anchor="s")
                nl = self.tk.Label(col, text=(s.get("note") or "")[:28], bg="#0d1117",
                                   fg="#7fa6d0", font=("Microsoft YaHei UI", self._f(8)),
                                   wraplength=self._p(104), justify="center")
                nl.pack(side="top", pady=(0, self._p(3)))
                cell = self.tk.Frame(col, bg="#0d1117")
                cell.pack(side="top")
                chip = self.tk.Label(cell, text="注", bg="#161b22", fg="#8b949e",
                                     width=3, height=2,
                                     font=("Microsoft YaHei UI", self._f(10), "bold"))
                chip.pack()
                num = self.tk.Label(cell, text="注释", bg="#0d1117", fg="#6b7280",
                                    font=("Microsoft YaHei UI", self._f(8)))
                num.pack()
                self.cells.append({"cell": cell, "pic": chip, "num": num, "col": col,
                                   "note": None, "img": None, "img_dim": None})
                continue
            heavy = bool(s.get("heavy"))
            kind = "heavy" if heavy else ("ult" if s.get("hold") else ("link" if str(s.get("key")).upper() == "E" else "skill"))
            op = int(s.get("op", 1) or 1)
            who = self.team[op - 1] if 0 < op <= len(self.team) else ""
            try:
                src = (core.icon("heavy", core.ELEMENT_COLORS.get("物理", (110, 114, 120)),
                                 "", "", badge=False)
                       if heavy else op_icon_png(who, kind, s.get("hold"), op))
                ph = self._photo(src)
            except Exception:
                ph = None
            col = self.tk.Frame(self.frame, bg="#0d1117")
            col.pack(side="left", padx=self._p(5), anchor="s")
            note = s.get("note") or ""
            note_lb = None
            if note:
                note_lb = self.tk.Label(col, text=note, bg="#0d1117", fg="#7fa6d0",
                                        font=("Microsoft YaHei UI", self._f(8)),
                                        wraplength=self._p(104), justify="center")
                note_lb.pack(side="top", pady=(0, self._p(3)))
            cell = self.tk.Frame(col, bg="#0d1117")
            cell.pack(side="top")
            pic = self.tk.Label(cell, bg="#0d1117")
            ph_dim = None
            if ph:
                pic.config(image=ph)
                pic.image = ph
                if self.overlay_style in ("focus", "both"):
                    try:
                        ph_dim = self._photo(src, True)
                    except Exception:
                        ph_dim = None
            pic.pack()
            # 下面这行字：1战技 / 连携 / 1终结
            k_skill = self.keys["skill"][op - 1] if 0 < op <= 4 else str(op)
            k_ult = self.keys["ult"][op - 1] if 0 < op <= 4 else ""

            if self.display_mode == "pad":
                slots = (self.pad or {}).get("slots") or []
                sym = _pad_label(slots[op - 1]) if 0 < op <= len(slots) else ""
                mod = _pad_label((self.pad or {}).get("modifier"))
                link_key = _pad_label((self.pad or {}).get("link"))
                if heavy:
                    tag = "重击"
                elif kind == "link":
                    tag = ("%s连携" % (link_key or "连携"))
                elif s.get("hold"):
                    tag = ("长按%s终结" % (sym or op))
                else:
                    tag = ("%s战技" % (sym or op))
            else:
                if heavy:
                    tag = "重击"
                elif kind == "link":
                    tag = "%s连携" % self.keys["link"]
                elif s.get("hold"):
                    tag = ("%s终结" % k_ult) if k_ult else ("长按%s终结" % k_skill)
                else:
                    tag = "%s战技" % k_skill
            num = self.tk.Label(cell, text=tag, bg="#0d1117", fg="#6b7280",
                                font=("Microsoft YaHei UI", self._f(8)))
            num.pack()
            self.cells.append({"cell": cell, "pic": pic, "num": num, "col": col,
                               "note": note_lb, "img": ph, "img_dim": ph_dim})
        # 接下来 2 格：提前看到下一步（淡一点、小一号）
        self.ghosts = []
        if self.next_preview:
            for _k in range(2):
                gc = self.tk.Frame(self.frame, bg="#0d1117")
                gc.pack(side="left", padx=self._p(4), anchor="s")
                gp = self.tk.Label(gc, bg="#0d1117")
                gp.pack()
                gt = self.tk.Label(gc, text="", bg="#0d1117", fg="#3a4048",
                                   font=("Microsoft YaHei UI", self._f(7)))
                gt.pack()
                self.ghosts.append({"col": gc, "pic": gp, "num": gt})
        self._bind_drag(self.frame)
        self._settle_passive()
        self._paint()
        try:
            self.root.after(250, self._prefetch_next)
        except Exception:
            pass

    def _paint_ghosts(self, steps):
        """刷新「接下来 2 格」：淡一点、小一号，提前看到下一步。"""
        if not self.ghosts:
            return
        order = [i for i, s in enumerate(steps)
                 if not s.get("br") and not s.get("divider")]
        try:
            cur = order.index(self.idx)
        except ValueError:
            cur = -1
        upcoming = order[cur + 1:cur + 3] if cur >= 0 else []
        for slot, g in enumerate(self.ghosts):
            if slot >= len(upcoming):
                try:
                    g["pic"].config(image="")
                    g["pic"].image = None
                    g["num"].config(text="")
                except Exception:
                    pass
                continue
            s = steps[upcoming[slot]]
            heavy = bool(s.get("heavy"))
            kind = "heavy" if heavy else ("ult" if s.get("hold")
                                          else ("link" if str(s.get("key")).upper() == "E" else "skill"))
            op = int(s.get("op", 1) or 1)
            who = self.team[op - 1] if 0 < op <= len(self.team) else ""
            ph = None
            try:
                src = (core.icon("heavy", core.ELEMENT_COLORS.get("物理", (110, 114, 120)),
                                 "", "", badge=False)
                       if heavy else op_icon_png(who, kind, s.get("hold"), op))
                ph = self._photo(src, dim=True, size=max(12, int(self.icon_px * 0.72)))
            except Exception:
                ph = None
            try:
                if ph:
                    g["pic"].config(image=ph)
                    g["pic"].image = ph
                else:
                    g["pic"].config(image="")
                    g["pic"].image = None
                if heavy:
                    tag = "重击"
                elif kind == "link":
                    tag = "连携"
                elif s.get("hold"):
                    tag = "终结"
                else:
                    tag = "战技"
                g["num"].config(text=tag)
            except Exception:
                pass

    def _paint(self):
        style = self.overlay_style
        dim_others = style in ("focus", "both")
        blue_box = style in ("box", "both")

        # 重击跟随它后面第一个「真正技能」的状态
        steps = self._seg_steps()
        follow = {}
        for i in range(len(steps) - 1, -1, -1):
            if steps[i].get("heavy") or self._is_note_only(steps[i]):
                follow[i] = follow.get(i + 1, None)
            else:
                follow[i] = i

        self._paint_ghosts(steps)

        for i, c in enumerate(self.cells):
            on = (i == self.idx) or (follow.get(i) == self.idx and follow.get(i) is not None)
            bg = "#1f6feb" if (on and blue_box) else "#0d1117"
            c["cell"].config(bg=bg)
            c["pic"].config(bg=bg)
            if dim_others and c.get("img") is not None:
                img = c["img"] if on else (c.get("img_dim") or c["img"])
                c["pic"].config(image=img)
                c["pic"].image = img
            c["num"].config(bg=bg,
                            fg=("#ffffff" if (on and blue_box) else ("#5a6068" if dim_others and not on else "#484f58")))
            try:
                c["col"].config(bg=bg)
                if c.get("note") is not None:
                    c["note"].config(bg=bg, fg="#ffffff" if on else "#7fa6d0")
            except Exception:
                pass

    def _bind_drag(self, w):
        """让窗口里所有控件都能拖动整条。"""
        try:
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._save_pos())
            for c in w.winfo_children():
                self._bind_drag(c)
        except Exception:
            pass

    def _drag_start(self, e):
        try:
            self._dx = e.x_root - self.root.winfo_x()
            self._dy = e.y_root - self.root.winfo_y()
        except Exception:
            pass

    def _drag_move(self, e):
        try:
            self.auto_follow = False      # 用户自己拖了，就别再自动跟随换屏
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        except Exception:
            pass

    def _save_pos(self):
        """松手后把运行条位置写进配置（x 绝对，y 用“距这块屏顶部”）。"""
        try:
            mons = enum_monitors(force=True)
            idx = getattr(self, "screen_idx", 0)
            mon = mons[idx] if 0 <= idx < len(mons) else mons[0]
            x = self.root.winfo_x()
            y = self.root.winfo_y() - mon["rect"][1]
            cfg = load_cfg()
            cfg["overlayX"] = int(x)
            cfg["overlayY"] = int(max(0, y))
            cfg["overlayMonitor"] = mon["name"]
            save_cfg(cfg)
            self.pos_x, self.pos_y = int(x), int(max(0, y))
            log("overlay pos saved %s,%s (%s)" % (int(x), int(max(0, y)), mon["name"]))
        except Exception as e:
            log("overlay save pos fail %s" % e)

    def _place(self):
        """摆到目标那块屏的工作区里：x 用记录值（没有就居中），y 是“距这块屏顶部”。"""
        self.root.update_idletasks()
        w = self.frame.winfo_reqwidth()
        h = self.frame.winfo_reqheight()
        mons = enum_monitors()
        idx = getattr(self, "screen_idx", 0)
        if not (0 <= idx < len(mons)):
            idx = pick_screen()
            self.screen_idx = idx
        l, t, r, b = mons[idx]["work"]
        if self.pos_x is None or int(self.pos_x) < 0:
            x = l + max(0, (r - l - w) // 2)
        else:
            x = int(self.pos_x)
        x = max(l, min(x, max(l, r - w)))
        y = t + max(0, int(self.pos_y))
        y = max(t, min(y, max(t, b - h)))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def set_screen(self, idx):
        """换屏：保持水平相对位置、纵向仍按“距顶部”算，然后重摆。"""
        try:
            mons = enum_monitors(force=True)
            if not (0 <= int(idx) < len(mons)):
                return
            idx = int(idx)
            old = mons[self.screen_idx] if 0 <= getattr(self, "screen_idx", 0) < len(mons) else None
            cx = self.root.winfo_x() + max(1, self.root.winfo_width()) // 2
            cy = self.root.winfo_y()
            rel_x = (cx - old["rect"][0]) if old else None
            rel_y = (cy - old["rect"][1]) if old else self.pos_y
            self.screen_idx = idx
            mon = mons[idx]
            if rel_x is not None:
                self.pos_x = mon["rect"][0] + rel_x - max(1, self.root.winfo_width()) // 2
            self.pos_y = max(0, int(rel_y))
            self._place()
            try:
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            log("overlay -> screen #%d %s" % (idx, mon["name"]))
        except Exception as e:
            log("overlay set_screen fail %s" % e)

    # ---- 输入 ----
    VK = {"1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "e": 0x45,
          "f8": 0x77, "f9": 0x78, "esc": 0x1B}

    def _bindings(self):
        """把配置翻译成 {动作: 虚拟键码}。"""
        out = {}
        for i in range(4):
            vk = key_to_vk(self.keys["skill"][i])
            if vk:
                out["skill%d" % (i + 1)] = vk
            vk = key_to_vk(self.keys["ult"][i])
            if vk:
                out["ult%d" % (i + 1)] = vk
        for act, name in (("link", self.keys["link"]),
                          ("exit", self.keys["exit"]),
                          ("reset", self.keys["reset"]),
                          ("alphaDown", self.keys["alphaDown"]),
                          ("alphaUp", self.keys["alphaUp"]),
                          ("loopToggle", self.keys["loopToggle"]),
                          ("loopJump", self.keys["loopJump"])):
            vk = key_to_vk(name)
            if vk:
                out[act] = vk
        return out

    def _hook(self):
        """GetAsyncKeyState 轮询：不依赖钩子权限，游戏在前台也有效。"""
        bind = self._bindings()
        log("bindings: " + ", ".join("%s=%s" % (k, v) for k, v in
                                     ((a, self.keys["skill"][int(a[-1]) - 1]) for a in bind if a.startswith("skill"))))

        def poll():
            import ctypes
            u = ctypes.windll.user32
            state = {a: False for a in bind}
            while self.running:
                alt_down = bool(u.GetAsyncKeyState(0x12) & 0x8000)   # Alt
                for act, vk in bind.items():
                    down = bool(u.GetAsyncKeyState(vk) & 0x8000)
                    if down and not state[act]:
                        state[act] = True
                        # 按住 Alt + 战技键 = 直接放终结技（不用长按）
                        if alt_down and act.startswith("skill"):
                            self.q.put(("altult", act[-1]))
                        else:
                            self.q.put(("down", act))
                    elif not down and state[act]:
                        state[act] = False
                        self.q.put(("up", act))
                time.sleep(0.012)

        threading.Thread(target=poll, daemon=True).start()
        log("input polling started (%d keys, mode=%s)" % (len(bind), self.input_mode))

        # 键鼠 + 手柄同时启用（没有绑定会自动跳过）
        if True:
            self._raw_map = self._build_raw_map()
            raw_map = self._raw_map

            if not raw_map:
                log("pad mode on but no bindings, use settings to learn buttons")
            else:
                log("pad bindings: %d" % len(raw_map))

                def poll_pad():
                    held = set()
                    silent = 0
                    while self.running:
                        try:
                            now = pad_pressed_now()
                        except Exception as e:
                            log("pad read fail %s" % e)
                            time.sleep(0.2)
                            continue
                        try:
                            if self._pad_dirty:
                                self._raw_map = self._build_raw_map()
                                self._pad_dirty = False
                                log("pad bindings reloaded: %d" % len(self._raw_map))
                            raw_map = self._raw_map
                        except Exception as e:
                            log("pad reload fail %s" % e)
                            time.sleep(0.2)
                            continue
                        if now and not held:
                            log("pad press: %s" % ",".join(sorted(now)))
                        if not now:
                            silent += 1
                            if silent in (600, 2400):     # 约 7s / 28s
                                try:
                                    d = pad_diag()
                                    rc = [(x.get("slot"), x.get("rc")) for x in d["winmm"]]
                                    xi = [(x.get("slot"), x.get("rc")) for x in d["xinput"] if "slot" in x]
                                    log("pad idle %ds winmm=%s xinput=%s" % (silent * 12 // 1000, rc, xi))
                                    if all(r != 0 for _, r in rc) and all(r != 0 for _, r in xi):
                                        log("WARN: 手柄被别的程序独占（游戏）。"
                                            "建议装 DS4Windows 把手柄转成 XInput，"
                                            "那样游戏和本程序可以同时读。")
                                except Exception as e:
                                    log("pad idle (diag fail %s)" % e)
                        else:
                            silent = 0
                        fresh = sorted(now - held,
                                       key=lambda r: 0 if "pad_mod" in (raw_map.get(r) or []) else 1)
                        for rid in fresh:
                            acts = raw_map.get(rid) or []
                            slots = [a for a in acts if a.startswith("slot")]
                            if slots:
                                # 面键：交给 Tk 线程判断是「战技」还是「连携」
                                for a in slots:
                                    self.q.put(("down", a))
                            else:
                                for a in acts:
                                    self.q.put(("down", a))
                        for rid in held - now:
                            acts = raw_map.get(rid) or []
                            for a in acts:
                                self.q.put(("up", a))
                        held = now
                        time.sleep(0.012)

                threading.Thread(target=poll_pad, daemon=True).start()

    q = queue.Queue()

    def _tick(self):
        if self._stop_req:
            self.close()
            return
        try:
            while True:
                kind, name = self.q.get_nowait()
                try:
                    self._handle(kind, name)
                except Exception as e:
                    log("handle error %s %s: %s" % (kind, name, e))
        except queue.Empty:
            pass
        if self.running:
            self.root.after(30, self._tick)

    def _handle(self, kind, name):
        if name == "pad_mod":
            self.pad_mod = (kind == "down")
            if self.pad_mod:
                # 修饰键在面键之后才按下：补启动（已经当连携放过的不补）
                for slot in list(self.pad_faces):
                    if slot in self.pad_link_hit:
                        continue
                    self._start_face(slot)
            return

        if name.startswith("slot"):
            slot = int(name[-1])
            if kind == "down":
                # 新的一轮按下：把上一轮「当连携放过」的标记先清掉。
                # 丢一次抬手事件（切窗口/重连手柄）不会让这个面键从此按不出战技。
                self.pad_link_hit.discard(slot)
                self.pad_faces[slot] = time.time()
                if self.pad_mod:
                    self._start_face(slot)
                elif self._face_is_link(slot):
                    # 这个面键顺便被设成连携键：没按修饰键 → 当连携用
                    self.pad_link_hit.add(slot)
                    self._fire("link", 1)
                else:
                    # 同时按下：等 130ms 看修饰键是否跟上
                    self.root.after(130, lambda s=slot: self._maybe_start_face(s))
                return
            self.pad_faces.pop(slot, None)
            key = "pad%02d" % slot
            t0 = self.press_at.pop(key, None)
            if slot in self.pad_link_hit:
                self.pad_link_hit.discard(slot)
                return
            if t0 is not None and (time.time() - t0) * 1000 < self.hold_ms:
                self._fire("skill", slot)
            return

        if name == "exit" and kind == "down":
            self.close()
            return
        if name == "alphaDown" and kind == "down":
            self._step_alpha(-0.10)
            return
        if name == "alphaUp" and kind == "down":
            self._step_alpha(0.10)
            return
        if name == "loopToggle" and kind == "down":
            # 临时开/关“反复记号”的效果（不改轴，只影响本次运行）
            self.loop_on = not self.loop_on
            if self.loop_on:
                self._reset_groups()
            self._status("反复 开" if self.loop_on else "反复 关", 1400)
            log("repeat %s" % ("on" if self.loop_on else "off"))
            return
        if name == "loopJump" and kind == "down":
            g, inrep = self._repeat_start_g()
            grp = self._enclosing(self._g())
            if grp:
                self.left[grp["id"]] = grp["times"]     # 手动回头 = 这一轮重新数
            self._go_g(g, "回反复起点" if inrep else "回本段开头")
            log("jump to repeat start g%d" % g)
            return
        if name == "reset" and kind == "down":
            self._jump_to(0, "回到第一段")
            log("reset to segment 0")
            return

        if name == "link":
            if kind == "down":
                self._fire("link", 1)
            return

        if name.startswith("ult"):
            if kind == "down":
                self._fire("ult", int(name[-1]))
            return

        if kind == "altult":
            # Alt + 1~4：按下瞬间算终结技（name 是 1~4）
            try:
                self._fire("ult", int(name))
            except Exception as e:
                log("altult fail %s" % e)
            return

        # skillN：短按=战技，长按=终结技
        if kind == "down":
            self.press_at[name] = time.time()
            self.root.after(int(self.hold_ms), lambda n=name: self._check_long(n))
            return
        t0 = self.press_at.pop(name, None)
        if t0 is None:
            return
        if (time.time() - t0) * 1000 < self.hold_ms:
            self._fire("skill", int(name[-1]))

    def _build_raw_map(self):
        """一个物理键可以挂多个动作（面键既能当战技，也能当连携）。"""
        m = {}
        if self.pad.get("modifier"):
            m.setdefault(self.pad["modifier"], []).append("pad_mod")
        for i, rid in enumerate(self.pad.get("slots") or []):
            if rid:
                m.setdefault(rid, []).append("slot%d" % (i + 1))
        if self.pad.get("link"):
            m.setdefault(self.pad["link"], []).append("link")
        return m

    def update_pad(self, raw):
        """设置里改了手柄键位，正在跑的条立即生效。"""
        self.pad = normalize_pad(raw)
        self._pad_dirty = True

    def _face_is_link(self, slot):
        """这个面键是不是也被设成了连携键。"""
        try:
            rid = (self.pad.get("slots") or [])[slot - 1]
            return bool(rid) and rid == self.pad.get("link")
        except Exception:
            return False

    def _start_face(self, slot):
        key = "pad%02d" % slot
        if key in self.press_at:
            return
        self.press_at[key] = time.time()
        self.root.after(int(self.hold_ms), lambda k=key, s=slot: self._check_long_pad(k, s))

    def _maybe_start_face(self, slot):
        if slot in self.pad_faces and self.pad_mod:
            self._start_face(slot)

    def _check_long_pad(self, key, slot):
        t0 = self.press_at.get(key)
        if t0 is None or not self.running:
            return
        if (time.time() - t0) * 1000 >= self.hold_ms - 5:
            self.press_at.pop(key, None)
            self._fire("ult", slot)

    def _check_long(self, name):
        t0 = self.press_at.get(name)
        if t0 is None or not self.running:
            return
        if (time.time() - t0) * 1000 >= self.hold_ms - 5:
            self.press_at.pop(name, None)
            self._fire("ult", int(name[-1]))

    def _seg_steps(self):
        return self.segments[self.seg][1] if self.segments else []

    @staticmethod
    def _is_note_only(step):
        """只有一句话、没有技能的格子（纯注释）。"""
        if not isinstance(step, dict) or not step.get("note"):
            return False
        if step.get("key") or step.get("hold") or step.get("heavy"):
            return False
        if step.get("divider") or step.get("br") or step.get("rep"):
            return False
        return True

    @staticmethod
    def _is_passive(step):
        """不用按键的步：重击提示、纯注释、换行这种。"""
        if not isinstance(step, dict):
            return True
        if step.get("heavy"):
            return True
        if step.get("divider") or step.get("rep"):
            return False
        return not (step.get("key") or step.get("hold"))

    def _settle_passive(self):
        """把 idx 挪过所有重击（不触发重绘，避免闪一下）。"""
        steps = self._seg_steps()
        while self.idx < len(steps) and self._is_passive(steps[self.idx]):
            self.idx += 1
        return self.idx >= len(steps)

    def _skip_passive(self):
        """跳过重击这类不用按键的步骤。返回是否发生了跳动。"""
        steps = self._seg_steps()
        moved = False
        while self.idx < len(steps) and self._is_passive(steps[self.idx]):
            self.idx += 1
            moved = True
        if moved:
            if self._maybe_repeat():
                self._render()
                self._place()
            elif self.idx >= len(steps):
                self._finish_segment()
            else:
                self._render()
        return moved

    def _menu(self, event=None):
        try:
            m = self.tk.Menu(self.root, tearoff=0)
            m.add_command(label="下一段", command=lambda: self._goto_seg(self.seg + 1))
            m.add_command(label="上一段", command=lambda: self._goto_seg(self.seg - 1))
            m.add_command(label="回到开头", command=self._goto_start)
            m.add_separator()
            m.add_command(label="打开主界面", command=self._focus_main)
            m.add_command(label="关闭运行条", command=self.close)
            # 先把运行条取消置顶并让设置立即生效，菜单才不会被压住
            try:
                self.root.attributes("-topmost", False)
                self.root.update_idletasks()
                self.root.update()
            except Exception:
                pass
            m.tk_popup(event.x_root, event.y_root)
            try:
                m.focus_force()
            except Exception:
                pass

            def _restore(*_a):
                try:
                    self.root.attributes("-topmost", True)
                except Exception:
                    pass

            try:
                m.bind("<Unmap>", _restore)
                self.root.after(3000, _restore)
            except Exception:
                pass
        except Exception as e:
            log("menu fail %s" % e)

    def _goto_seg(self, n):
        if not self.segments:
            return
        self.seg = max(0, min(len(self.segments) - 1, n))
        self.idx = 0
        self._render()
        self._place()
        self._bind_drag(self.frame)

    def _goto_start(self):
        self.seg = 0
        self.idx = 0
        self.passes = 0
        self._render()
        self._place()
        self._bind_drag(self.frame)

    def _focus_main(self):
        try:
            import ctypes
            u = ctypes.windll.user32
            u.FindWindowW.restype = ctypes.c_void_p
            hwnd = u.FindWindowW(None, "Endfield RotPilot")
            if hwnd:
                u.ShowWindow(ctypes.c_void_p(hwnd), 9)
                u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        except Exception as e:
            log("focus main fail %s" % e)

    # ---- 段名标签临时显示一句话（循环/透明度提示） ----
    def _status(self, text, ms=1400):
        lb = self._tag_lb
        if lb is None:
            return
        try:
            if self._tag_job:
                self.root.after_cancel(self._tag_job)
        except Exception:
            pass
        try:
            lb.config(text=text, fg="#7ee787")
        except Exception:
            return

        def back():
            try:
                lb.config(text=self._tag_text, fg="#ffd400")
            except Exception:
                pass
            self._tag_job = None
        try:
            self._tag_job = self.root.after(ms, back)
        except Exception:
            pass

    # ---- 透明度（外部 HTTP 调用也走这里，线程安全） ----
    def _apply_alpha(self, v):
        self._alpha = clamp_alpha(v)
        try:
            self.root.attributes("-alpha", self._alpha)
        except Exception:
            pass

    def push_alpha(self, v):
        """给 HTTP 线程用：排到 Tk 线程里改，别跨线程动窗口。"""
        v = clamp_alpha(v)
        try:
            self.root.after(0, lambda: self._apply_alpha(v))
        except Exception:
            pass

    def _step_alpha(self, delta):
        self._apply_alpha(self._alpha + delta)
        self._status("透明度 %d%%" % int(round(self._alpha * 100)), 1100)

    # ---- 反复（乐谱式记号，支持嵌套） ----
    def _reset_groups(self):
        """所有反复区间恢复满次数（-1 = 无限，不减）。"""
        self.left = {g["id"]: g["times"] for g in getattr(self, "rep_groups", [])}
        self.rep_done = {}          # 每个区间已经“回头”过几次（无限循环时用来显示第几遍）

    def _g(self):
        """当前全局步号。"""
        if not self.segments:
            return 0
        return self.seg_base[self.seg] + self.idx

    def _split_g(self, g):
        """全局步号 → (段, 段内下标)；g 可以等于总步数（表示一段刚跑完）。"""
        g = max(0, min(g, getattr(self, "total", 0)))
        seg = 0
        for i in range(len(self.segments) - 1, -1, -1):
            if g >= self.seg_base[i]:
                seg = i
                break
        return seg, g - self.seg_base[seg]

    def _go_g(self, g, note=""):
        """跳到全局步号 g；换段了就走换段流程（重建控件），否则只重画。"""
        seg, idx = self._split_g(g)
        changed = seg != self.seg
        self.seg, self.idx = seg, idx
        self.press_at.clear()
        if changed:
            self._after_seg_change(note)
        else:
            self._paint()
            if note:
                self._status(note, 1200)

    def _enclosing(self, g):
        """包住 g 的最内层反复区间。"""
        hit = None
        for x in getattr(self, "rep_groups", []):
            if x["start"] <= g <= x["end"] + 1:
                if hit is None or int(x.get("depth") or 0) > int(hit.get("depth") or 0):
                    hit = x
        return hit

    def _repeat_start_g(self):
        """F11 的目标：最内层反复的起点；不在反复里就回本段开头。"""
        grp = self._enclosing(self._g())
        if grp:
            return grp["start"], True
        return self.seg_base[self.seg] if self.segments else 0, False

    def _maybe_repeat(self):
        """刚跑完某个反复区间的最后一格 → 跳回起点（内层优先）。"""
        if not self.loop_on or not getattr(self, "rep_groups", []):
            return False
        g = self._g()
        # 刚跨过的位置：g-1。可能同时是好几层（嵌套且终点重合）的结尾
        cands = [x for x in self.rep_groups if x["end"] == g - 1]
        cands.sort(key=lambda x: -int(x.get("depth") or 0))   # 层数深的 = 更内层，先处理
        for grp in cands:
            gid, total = grp["id"], int(grp["times"])
            left = self.left.get(gid, 1)
            if total > 0 and left <= 1:
                continue                                    # 最后一遍：放行
            if total < 0:
                # 无限循环：次数不减，只记“跑过几遍”用来显示
                self.rep_done[gid] = self.rep_done.get(gid, 0) + 1
                done = self.rep_done[gid] + 1
            else:
                self.left[gid] = left - 1
                done = total - self.left[gid] + 1
            # 内层次数复位，这样外层每转一圈，内层都重新打满
            for inner in self.rep_groups:
                if inner is grp:
                    continue
                if (int(inner.get("depth") or 0) > int(grp.get("depth") or 0)
                        and inner["start"] >= grp["start"] and inner["end"] <= grp["end"]):
                    self.left[inner["id"]] = inner["times"]
                    self.rep_done.pop(inner["id"], None)
            note = ("反复 ∞（第 %d 遍）" % done) if total < 0 else ("反复 %d/%d" % (done, total))
            if len(self.rep_groups) > 1:
                note += " · 第%d层" % (int(grp.get("depth") or 0) + 1)
            self._go_g(grp["start"], note)
            log("repeat %s (%s) -> g%d" % (gid, "inf %d" % done if total < 0 else "%d/%d" % (done, total),
                                           grp["start"]))
            return True
        return False

    def _after_seg_change(self, note=""):
        """换段统一入口：藏一下重建控件，避免闪。"""
        def go():
            try:
                self.root.withdraw()
            except Exception:
                pass
            self._render()
            self._place()
            self._bind_drag(self.frame)
            try:
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            if note:
                self._status(note, 1500)
        self.root.after(120, go)

    def _jump_to(self, seg, note=""):
        if not self.segments:
            return
        self.seg = max(0, min(len(self.segments) - 1, seg))
        self.idx = 0
        self.press_at.clear()
        self._reset_groups()
        self._after_seg_change(note)

    def _finish_segment(self):
        """本段跑完：有下一段就切，否则收起。反复由轴上的反复记号控制，不在这里。"""
        if self.seg + 1 < len(self.segments):
            self.seg += 1
            self.idx = 0
            self._after_seg_change()
            return
        self.root.after(350, self.close)

    def _fire(self, action, slot):
        if self._skip_passive():
            return

        steps = self._seg_steps()
        if self.idx >= len(steps):
            return
        s = steps[self.idx]

        if s.get("heavy"):
            return

        if action == "link":
            want_key, want_hold = "E", False
        elif action == "ult":
            want_key, want_hold = str(slot), True
        else:
            want_key, want_hold = str(slot), False

        if str(s.get("key")).upper() == want_key.upper() and bool(s.get("hold")) == want_hold:
            self.idx += 1
            done = self._settle_passive()
            if self._maybe_repeat():          # 跑完反复区间最后一格 → 跳回起点
                self._paint()
                return
            if done:
                self._finish_segment()
                return
            self._paint()

    def close(self):
        try:
            log("overlay closed at seg %d idx %d" % (self.seg, self.idx))
        except Exception:
            pass
        self.running = False
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.root.quit()          # 先停主循环，避免 destroy 之后 after 回调报错
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


_overlay = None

# ---------- 主窗口关掉之后自动收摊 ----------
# 主窗口（Edge --app 那个）一关，后台进程和那个最小化的黑窗口就没用了。
# 规则：主窗口关了 + 运行条没在跑 → 直接退出（黑窗口跟着关掉）。
#       运行条还在跑就先留着，等它结束再退，不然游戏里的提示条会突然消失。
_MAIN = {"gone": False, "last": time.time(), "seen": False, "why": ""}
_EXITING = [False]


def main_touch():
    """页面还在（打开或心跳）→ 记一下。"""
    _MAIN["gone"] = False
    _MAIN["seen"] = True
    _MAIN["last"] = time.time()


def main_gone(why="主窗口关闭"):
    # 无浏览器模式（自检/调试）不该被当成"用户关了窗口"
    if os.environ.get("ROTPILOT_NO_BROWSER") or os.environ.get("ROTPILOT_NO_AUTO_EXIT"):
        return
    # 默认不自动退出：只有设置里明确打开才关（以前默认开，关窗口就断连）
    try:
        if not int(load_cfg().get("autoExit") or 0):
            return
    except Exception:
        return
    _MAIN["gone"] = True
    _MAIN["why"] = why
    # 别立刻退出：Ctrl+R 刷新时也会触发 pagehide，留 2.5 秒给页面重新连上来
    threading.Timer(2.5, lambda: maybe_quit(why)).start()


def overlay_busy():
    ov = _overlay
    return bool(ov is not None and getattr(ov, "running", False))


def maybe_quit(why="空闲"):
    """主窗口没了、运行条也没在跑 → 收摊。返回是否已经安排了退出。"""
    if os.environ.get("ROTPILOT_NO_BROWSER") or os.environ.get("ROTPILOT_NO_AUTO_EXIT"):
        return False
    try:
        if not int(load_cfg().get("autoExit") or 0):
            return False
    except Exception:
        return False
    if not _MAIN.get("gone") or overlay_busy():
        return False
    if _EXITING[0]:
        return True
    _EXITING[0] = True
    log("auto exit: %s" % why)
    threading.Thread(target=_hard_exit, daemon=True).start()
    return True


def _hard_exit():
    time.sleep(0.6)      # 让还没写完的响应/日志先落地
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)          # python 一退，那个最小化的黑窗口也跟着关


def _watchdog():
    """兜底：页面心跳断了很久（浏览器崩了 / 被强杀）也收摊。
    阈值比后台节流还大（窗口隐藏时定时器会被压到 1 分钟一次）。"""
    while True:
        time.sleep(5)
        if _EXITING[0]:
            return
        if not _MAIN.get("seen"):
            continue
        quiet = time.time() - _MAIN["last"]
        if quiet > 100 and not overlay_busy():
            main_gone("主窗口 %d 秒没响应" % int(quiet))
            return


_OVERLAY_LOCK = threading.Lock()
_OVERLAY_BUSY = [False]         # 已经有人在起运行条（防手快连点出两条）


def start_overlay(cfg):
    global _overlay
    # 判断和起线程之间必须上锁：以前没锁，连点两次「开始运行」会起出两条运行条
    with _OVERLAY_LOCK:
        if _OVERLAY_BUSY[0]:
            log("overlay already starting, ignore")
            return False
        if _overlay and _overlay.running:
            return False
        _OVERLAY_BUSY[0] = True

    def _run():
        global _overlay
        try:
            # 键鼠 / 手柄同时启用：Raw Input 一直尝试挂上
            raw_pad()
            _overlay = Overlay(cfg.get("steps") or [], cfg.get("team") or [],
                               int(cfg.get("holdMs", 280)), clamp_alpha(cfg.get("opacity", 0.94)),
                               int(cfg.get("iconSize") or 60),
                               cfg.get("overlayX"),
                               int(cfg.get("overlayY") if cfg.get("overlayY") is not None else 150),
                               cfg.get("keys"),
                               cfg.get("inputMode") or "kb",
                               cfg.get("pad"),
                               cfg.get("overlayStyle") or "focus",
                               int(cfg.get("loopCount", 0) or 0),
                               cfg.get("displayMode") or "kb",
                               int(cfg.get("loopFrom", 0) or 0),
                               int(cfg.get("loopTo", 0) or 0),
                               screen=pick_screen(cfg),
                               ui_scale=cfg.get("uiScale") or 1,
                               next_preview=(str(cfg.get("nextPreview", 1))
                                             not in ("0", "False", "false", "")))
            _overlay.run()
        except Exception as e:
            log("overlay err %s" % e)
        finally:
            _overlay = None
            _OVERLAY_BUSY[0] = False
            # 运行条结束了：主窗口早就关了的话，这里顺手收摊（黑窗口一起关掉）
            maybe_quit("运行条结束")

    threading.Thread(target=_run, daemon=True).start()
    return True


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"       # 连接复用，避免并发请求时被断开
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _file(self, path):
        if not os.path.exists(path):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    def _backup_rel(self, rel, stamp):
        """把程序目录里的某个文件备份到 _backup/<stamp>/ 下。"""
        src = os.path.join(BASE, *rel.split("/"))
        if not os.path.exists(src):
            return
        dst = os.path.join(BACKUP_DIR, stamp, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            log("backup fail %s: %s" % (rel, e))

    def _patch_import(self, raw):
        """接收补丁 zip：备份旧文件后覆盖（runtime 不允许改）。"""
        if not raw or len(raw) < 20:
            return self._send(400, {"ok": False, "error": "空文件"})
        if len(raw) > 40 * 1024 * 1024:
            return self._send(400, {"ok": False, "error": "文件太大"})
        try:
            import io as _io
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception as e:
            return self._send(400, {"ok": False, "error": "不是有效的 zip：%s" % e})

        stamp = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(os.path.join(BACKUP_DIR, stamp), exist_ok=True)
        done, skipped, need_restart = [], [], False

        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or name.startswith("/") or ".." in parts:
                skipped.append(name)
                continue
            if parts[0].lower() == "runtime":
                skipped.append(name)
                continue
            if parts[0] in ("_backup", "_补丁备份"):
                skipped.append(name)
                continue
            # 用户数据不能覆盖：轴、编队、配置一律跳过（补丁只该改程序文件）
            low = "/".join(parts).lower()
            if low in ("config.json", "config.json.bak", "web_run.log"):
                skipped.append(name)
                continue
            try:
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    skipped.append(name)
                    continue
            except Exception:
                pass

            rel = "/".join(parts)
            self._backup_rel(rel, stamp)
            dest = os.path.join(BASE, *parts)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                done.append(rel)
                if rel.lower().endswith(".py"):
                    need_restart = True
            except Exception as e:
                log("patch write fail %s: %s" % (rel, e))
                skipped.append(rel)

        # 记录这次备份包含哪些文件，方便还原
        try:
            with open(os.path.join(BACKUP_DIR, stamp, "_files.json"), "w", encoding="utf-8") as f:
                json.dump({"stamp": stamp, "files": done}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        log("patch applied: %d files, restart=%s, backup=%s" % (len(done), need_restart, stamp))
        return self._send(200, {"ok": True, "files": done, "skipped": skipped,
                                "restart": need_restart, "backup": stamp})

    def _latest_backup(self):
        if not os.path.isdir(BACKUP_DIR):
            return None
        stamps = [d for d in os.listdir(BACKUP_DIR)
                  if os.path.isdir(os.path.join(BACKUP_DIR, d))]
        if not stamps:
            return None
        stamps.sort()
        return stamps[-1]

    def _patch_restore(self):
        """把最近一次补丁覆盖过的文件还原回去。"""
        stamp = self._latest_backup()
        if not stamp:
            return self._send(200, {"ok": False, "error": "没有可还原的备份"})
        root = os.path.join(BACKUP_DIR, stamp)
        restored, need_restart = [], False
        for base_dir, _dirs, files in os.walk(root):
            for fn in files:
                if fn == "_files.json":
                    continue
                full = os.path.join(base_dir, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                dest = os.path.join(BASE, *rel.split("/"))
                try:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(full, dest)
                    restored.append(rel)
                    if rel.lower().endswith(".py"):
                        need_restart = True
                except Exception as e:
                    log("restore fail %s: %s" % (rel, e))
        log("patch restored from %s: %d files" % (stamp, len(restored)))
        return self._send(200, {"ok": True, "restored": restored,
                                "restart": need_restart, "backup": stamp})

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = unquote(u.path)

        if p in ("/", "/index.html"):
            main_touch()          # 主窗口开了（包括右键菜单重新打开）
            return self._file(os.path.join(BASE, "ui", "index.html"))
        if p == "/favicon.ico":
            return self._file(os.path.join(BASE, "ui", "favicon.ico"))
        if p in ("/icon.png", "/apple-touch-icon.png"):
            return self._file(os.path.join(BASE, "ui", "icon.png"))
        if p.startswith("/ui/"):
            return self._file(os.path.join(BASE, p.lstrip("/")))

        if p == "/api/update/operators/check":
            return self._send(200, update_ops_check())

        if p == "/api/update/check":
            return self._send(200, update_check())

        if p == "/api/update/progress":
            path = _UPDATE.get("path") or ""
            exists = bool(path) and os.path.exists(path)
            out = dict(_UPDATE)
            out["exists"] = exists
            out["size"] = os.path.getsize(path) if exists else 0
            return self._send(200, out)

        if p == "/api/pad_debug":
            raw_pad()
            out = {}
            try:
                rp = _RAW_PAD
                if rp is not None:
                    for dev, data in rp.snapshot().items():
                        out[str(dev)] = data.hex() if data else ""
            except Exception as e:
                out["error"] = str(e)
            return self._send(200, {"raw": out, "xbox": sorted(_xbox_raw_ids(b"\x01" + bytes(16)))})

        if p == "/api/pad_state":
            raw_pad()          # 设置面板打开时也要能读到（不用先点开始运行）
            try:
                pressed = sorted(pad_pressed_now())
            except Exception:
                pressed = []
            return self._send(200, {"pressed": pressed, "devices": pad_devices()})

        if p == "/api/whoami":
            main_touch()          # 页面每 4 秒来一次，等于“主窗口还活着”
            # admin 一起带回去：没拿到管理员权限时游戏里的按键读不到，界面要提示
            return self._send(200, {"app": "rotpilot", "port": PORT,
                                    "admin": bool(_is_admin())})

        if p == "/api/about":
            # 版本 / 是不是打过补丁 / 这一版更新了什么
            bak = os.path.join(BASE, "_补丁备份")
            patched = os.path.isdir(bak)
            patch_time = ""
            if patched:
                try:
                    dirs = sorted(d for d in os.listdir(bak)
                                  if os.path.isdir(os.path.join(bak, d)))
                    patch_time = dirs[-1] if dirs else ""
                except Exception:
                    patch_time = ""
            notes = ""
            doc = os.path.join(BASE, "本次更新.txt")
            if os.path.exists(doc):
                try:
                    with open(doc, "r", encoding="utf-8-sig", errors="replace") as f:
                        notes = f.read()[:6000]
                except Exception:
                    notes = ""
            # 有补丁备份 = 打过补丁；剩下的都是我们发的集成版（代码里带版本号）
            mode = "patched" if patched else "integrated"
            return self._send(200, {"version": APP_VERSION, "build": APP_BUILD,
                                    "mode": mode, "patched": patched,
                                    "patchTime": patch_time, "notes": notes})

        if p == "/api/last-error":
            # 出错时给用户一个“能截图给作者”的东西，不然只能看到“后台没响应”
            try:
                path = os.path.join(os.path.dirname(CFG) or BASE, "web_run.log")
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-40:]
                txt = "".join(lines).strip()
            except Exception as e:
                txt = "读不到日志：%s" % e
            return self._send(200, {"log": txt})

        if p == "/api/screens":
            mons = enum_monitors(force=True)
            cfg = load_cfg()
            g = game_window()
            cur = -1
            hwnd = _find_main_hwnd()
            if hwnd:
                cur = _mon_of_window(hwnd, mons)
            return self._send(200, {
                "mode": cfg.get("screenMode") or "game",
                "monitors": mons,
                "current": cur,
                "target": pick_screen(cfg, mons),
                "game": {"found": bool(g["hwnd"]),
                         "screen": _mon_of_window(g["hwnd"], mons) if g["hwnd"] else -1,
                         "title": g["title"], "exe": g["exe"], "sure": g["sure"]},
            })

        if p == "/api/state":
            return self._send(200, load_cfg())

        if p == "/api/ops":
            _refresh_roster_if_changed()
            out = []
            for i, n in enumerate(core.OPS):
                if not n:
                    continue
                out.append({
                    "slot": i,
                    "name": n,
                    "element": read_txt(core.op_file(n, "element.txt")),
                    "cls": read_txt(core.op_file(n, "class.txt")),
                    "card": bool(card_path(n)),
                    "avatar": bool(avatar_path(n)),
                    "assets": {
                        "skill": has_asset(n, "skill"),
                        "link": has_asset(n, "link"),
                        "ult": has_asset(n, "ult"),
                    },
                })
            return self._send(200, out)

        if p == "/img/heavy":
            try:
                path = core.icon("heavy", core.ELEMENT_COLORS.get("物理", (110, 114, 120)),
                                 "", "", badge=False)
                return self._file(path)
            except Exception as e:
                log("heavy icon fail %s" % e)
                return self._send(404, {"error": "no heavy"})

        if p == "/img/icon":
            name = (q.get("name") or [""])[0]
            kind = (q.get("kind") or ["skill"])[0]
            hold = (q.get("hold") or ["0"])[0] == "1"
            slot = int((q.get("slot") or ["1"])[0] or 1)
            # 没有专属素材 → 只画底层（属性色块 + 右下头像）
            if not name:
                return self._send(404, {"error": "no operator"})
            if not has_asset(name, kind):
                try:
                    return self._file(base_icon(name, kind, slot))
                except Exception as e:
                    log("base icon fail %s" % e)
                    return self._send(404, {"error": "no asset"})
            try:
                path = op_icon_png(name, kind, hold, slot)
                return self._file(path)
            except Exception as e:
                log("icon fail %s" % e)
                return self._send(404, {"error": "icon"})

        if p == "/img/card":
            name = (q.get("name") or [""])[0]
            path = card_path(name)
            if path:
                return self._file(path)
            return self._send(404, {"error": "card"})

        if p == "/img/avatar":
            name = (q.get("name") or [""])[0]
            path = avatar_path(name)
            if path:
                return self._file(path)
            return self._send(404, {"error": "avatar"})

        return self._send(404, {"error": "no route"})

    @staticmethod
    def _origin_ok(origin):
        """只信本机页面；别的网站就算偷偷 POST 过来也不理。"""
        if not origin:
            return True
        try:
            from urllib.parse import urlparse as _up
            host = _up(origin).hostname or ""
            return host in ("127.0.0.1", "localhost", "::1")
        except Exception:
            return False

    @staticmethod
    def _only_list(v):
        """只接受 list，其他一律当没传。"""
        return v if isinstance(v, list) else None

    def do_POST(self):
        global _overlay
        try:
            return self._do_post_inner()
        except Exception as e:
            log("POST %s failed: %s" % (self.path, e))
            try:
                self._send(500, {"ok": False, "error": str(e)})
            except Exception:
                pass

    def _do_post_inner(self):
        global _overlay          # 停机时要把全局引用清掉，少了这句只会改到局部变量
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}

        # 防站着把钱抢了：只接受本机页面发来的 POST
        origin = self.headers.get("Origin") or ""
        if not self._origin_ok(origin):
            return self._send(403, {"ok": False, "error": "forbidden origin"})
        if u.path in ("/api/state", "/api/run", "/api/quit", "/api/restart"):
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ctype:
                return self._send(403, {"ok": False, "error": "need json"})

        if u.path == "/api/state":
            cfg = load_cfg()
            # 防误清：空轴不许覆盖有内容的轴（除非明确 force）
            if (isinstance(body.get("steps"), list) and not body["steps"]
                    and (cfg.get("steps") or []) and not body.get("force")):
                body = {k: v for k, v in body.items() if k != "steps"}
                log("refused empty steps overwrite (%d steps kept)" % len(cfg["steps"]))
            for k in ("holdMs", "opacity", "iconSize", "overlayX", "overlayY",
                      "overlayStyle", "padType", "loopCount", "loopFrom", "loopTo",
                      "displayMode", "inputMode", "screenMode", "uiScale", "nextPreview",
                      "autoExit"):
                if k in body:
                    cfg[k] = body[k]
            for k in ("steps", "team", "teams", "comps"):
                if k in body:
                    v = self._only_list(body[k])
                    if v is None:
                        log("ignore bad %s type: %s" % (k, type(body[k]).__name__))
                    else:
                        cfg[k] = v
            if "keys" in body:
                cfg["keys"] = body["keys"] if isinstance(body["keys"], dict) else cfg.get("keys")
            if "pad" in body:
                cfg["pad"] = body["pad"] if isinstance(body["pad"], (dict, type(None))) else cfg.get("pad")
            cfg["opacity"] = clamp_alpha(cfg.get("opacity", ALPHA_DEFAULT))
            cfg["keys"] = normalize_keys(cfg.get("keys"))
            save_cfg(cfg)
            # 手柄键位改了：正在跑的运行条立即换绑，不用重开
            if overlay_busy():
                try:
                    _overlay.update_pad(cfg.get("pad"))
                except Exception as e:
                    log("pad hot reload fail %s" % e)
            return self._send(200, {"ok": True, "opacity": cfg["opacity"]})

        if u.path == "/api/patch_restore":
            return self._patch_restore()

        if u.path == "/api/patch_import":
            return self._patch_import(raw)

        if u.path == "/api/restart":
            self._send(200, {"ok": True})
            log("restart requested")
            threading.Timer(0.5, _restart_self).start()
            return

        if u.path == "/api/quit":
            self._send(200, {"ok": True})
            log("quit requested")
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return

        if u.path == "/api/update/operators/apply":
            url = str(body.get("url") or "")
            name = str(body.get("name") or "operators.zip")
            if not url:
                return self._send(400, {"ok": False, "error": "no url"})
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "EndfieldRotPilot"})
                data = urllib.request.urlopen(req, timeout=60).read()
            except Exception as e:
                return self._send(500, {"ok": False, "error": "下载失败：%s" % e})
            ok, info, err = import_operators_pack(data, name)
            if not ok:
                return self._send(500, {"ok": False, "error": err or "导入失败"})
            cfg = load_cfg()
            cfg["opPack"] = {"name": name, "at": time.strftime("%Y-%m-%d %H:%M")}
            save_cfg(cfg)
            log("operators pack imported: %s" % name)
            return self._send(200, {"ok": True, "result": info})

        if u.path == "/api/update/download":
            url = str(body.get("url") or "")
            log("update download requested: %s" % (url[:120] or "(empty)"))
            if not url or not url.lower().startswith("http"):
                return self._send(400, {"ok": False, "error": "没有下载地址"})
            update_download(url)
            return self._send(200, {"ok": True})

        if u.path == "/api/update/apply":
            ok, info = update_apply()
            if not ok:
                return self._send(400, {"ok": False, "error": info})
            threading.Timer(0.8, lambda: os._exit(0)).start()
            return self._send(200, {"ok": True, "bat": info})

        if u.path == "/api/stop":
            ok = False
            ov = _overlay
            if ov is not None and getattr(ov, "running", False):
                ov._stop_req = True          # 交给 Tk 线程自己关，别跨线程调用
                for _ in range(30):
                    if not getattr(ov, "running", False):
                        break
                    time.sleep(0.03)
                ok = True
            _overlay = None
            log("stopped by button")
            maybe_quit("运行条结束")
            return self._send(200, {"ok": ok})

        if u.path == "/api/bye":
            # 页面在关（sendBeacon 发的）。主窗口没了就先记下来，
            # 运行条没在跑的话 2.5 秒后自动退出，黑窗口一起关掉。
            main_gone("主窗口关闭")
            return self._send(200, {"ok": True, "exiting": _EXITING[0]})

        if u.path == "/api/screen":
            try:
                return self._send(200, apply_screen(body.get("mode")))
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/overlay/alpha":
            # 透明度实时调节：运行条在跑就直接改，没跑就只存配置
            v = clamp_alpha(body.get("opacity", ALPHA_DEFAULT))
            cfg = load_cfg()
            cfg["opacity"] = v
            save_cfg(cfg)
            running = overlay_busy()
            if running:
                try:
                    _overlay.push_alpha(v)
                except Exception:
                    pass
            return self._send(200, {"ok": True, "opacity": v, "running": running})

        if u.path == "/api/run":
            cfg = load_cfg()
            if "steps" in body:
                v = self._only_list(body["steps"])
                if v is not None:
                    cfg["steps"] = v
                else:
                    log("run: ignore bad steps type %s" % type(body["steps"]).__name__)
            if "team" in body:
                v = self._only_list(body["team"])
                if v is not None:
                    cfg["team"] = v
            save_cfg(cfg)
            ok = start_overlay(cfg)
            # ok=False 不是出错：多半是已经在跑了（或者上一条还没起完），说清楚就行
            dup = rep_dup_starts(cfg.get("steps") or [])
            return self._send(200, {"ok": ok,
                                    "why": "" if ok else "技能条已经在运行了",
                                    "warn": ("反复记号有重复的起点（%s），那一组会跑成超长反复"
                                             % "、".join(dup)) if dup else ""})

        if u.path == "/api/share":
            cfg = load_cfg()
            if "steps" in body:
                cfg["steps"] = body["steps"]
            if "team" in body:
                cfg["team"] = body["team"]
            if "axisName" in body:
                cfg["axisName"] = str(body.get("axisName") or "")[:40]
            # 长按毫秒跟着这条轴一起走（以前只读盘上的值，导出的码可能带着别的轴的长按）
            if "holdMs" in body:
                try:
                    cfg["holdMs"] = max(80, min(2000, int(body.get("holdMs"))))
                except Exception:
                    pass
            # fmt：rp2 兼容 / rp3 压缩（ver+slim+zip 是旧参数，前端没更新时还能用）
            try:
                ver = int(body.get("ver") or 2)
            except Exception:
                ver = 2
            slim = bool(body.get("slim"))
            pack = bool(body.get("zip"))
            fmt = str(body.get("fmt") or "").strip().lower() or None
            if fmt not in ("rp2", "rp3"):
                fmt = None
            try:
                code = share_encode(cfg, ver, slim=slim, pack=pack, fmt=fmt)
                eff = fmt or ("rp3" if (ver >= 3 or pack) else "rp2")
                resp = {"code": code, "fmt": eff,
                        "ver": 3 if code.startswith("RP3") else 2,
                        "slim": False, "pack": eff == "rp3",
                        "name": cfg.get("axisName") or ""}
                # 分享码里编队只存了「名单里的第几号」，不在名单里的人会写成空位
                known = [n for n in core.OPS if n]
                miss = [n for n in (cfg.get("team") or [])
                        if isinstance(n, str) and n and n not in known]
                if miss:
                    resp["warn"] = ("编队里的「%s」不在干员名单里，导出的码会把这位写成空位，"
                                    "对方导入后这几个技能没人" % "、".join(miss))
                return self._send(200, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/import_operator":
            # 注意：body 已经在函数开头读进 raw 了，这里不能重复读，否则会卡住
            try:
                ok, res = import_operator_zip(raw, self.headers.get("X-Filename") or "")
            except Exception as e:
                import traceback
                log("import crash: " + traceback.format_exc())
                return self._send(500, {"ok": False, "error": str(e)})
            if ok:
                return self._send(200, {"ok": True, **res})
            return self._send(400, {"ok": False, "error": res})

        if u.path == "/api/demo":
            # 示例轴不再自动塞进新装；轴库右上「示例」按钮点一下才载入
            try:
                return self._send(200, {"axes": demo_axes(), "comp": DEMO_COMP})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if u.path == "/api/import":
            raw_text = str(body.get("code") or "")
            # 粘贴的是「导出 JSON」那种文件内容？直接当一条轴导进来
            head = raw_text.strip()[:1]
            if head in ("{", "["):
                try:
                    jd = json.loads(raw_text)
                except Exception:
                    jd = None
                if isinstance(jd, dict) and isinstance(jd.get("teams"), list):
                    return self._send(400, {"error": "这是轴库备份文件，"
                                                     "请到「技能轴库 → 恢复」里用它"})
                if isinstance(jd, dict) and isinstance(jd.get("steps"), list):
                    tm = [str(x or "") for x in (jd.get("team") or [])][:4]
                    tm += [""] * (4 - len(tm))
                    return self._send(200, {"team": tm, "steps": jd["steps"],
                                            "holdMs": jd.get("holdMs") or 280,
                                            "name": str(jd.get("axisName")
                                                        or jd.get("name") or "")[:40],
                                            "from": "json"})
            try:
                data = share_decode(raw_text)
            except Exception as e_first:
                # 粘贴的可能是「兄弟这个轴给你 RP2:xxx 你试试」整段话 ——
                # 从里面把码捞出来再试一次，捞不出来再把原来的错误抛出去
                picked = _pick_code(raw_text)
                if not picked or picked == raw_text.strip():
                    raise
                log("import: picked code out of %d chars" % len(raw_text))
                try:
                    data = share_decode(picked)
                except Exception:
                    raise e_first
            try:
                # 反复记号里同一组写了两个起点：后端只认第一个，那段会变成超长反复。
                # 不拦，但必须说一声，否则用户完全不知道发生了什么。
                dup = rep_dup_starts(data.get("steps") or [])
                if dup:
                    data["warn"] = ("反复记号里有重复的起点（%s），那一段会被拉长成"
                                    "超长反复；删掉多余的起点再导入" % "、".join(dup))
                return self._send(200, data)
            except Exception as e:
                # 分享码被截断 / 复制漏了字符时，报“坏字符”“越界”太抽象，给句人话
                msg = str(e)
                low = msg.lower()
                if ("base85" in low or isinstance(e, (IndexError, ValueError))
                        or "unpack" in low or "range" in low):
                    if "版本" in msg or "太长" in msg:
                        msg = msg                      # 版本不支持 / 太长，原样给人看
                    else:
                        msg = "分享码不完整或已损坏（复制时可能漏了字符），请重新复制一遍"
                log("import fail: %s" % e)
                return self._send(400, {"error": msg})

        return self._send(404, {"error": "no route"})


# ---------------- 多显示器 ----------------
# 目标：主窗口（Edge --app 那个）和运行条都摆到「游戏所在的那块屏」，并记住上次的位置大小。
# 坐标一律用物理像素：进程按 per-monitor DPI aware 启动，Win32 给的就是物理像素，
# 所以主副屏缩放不同（150% / 100%）也不会漂移、不会跑到屏幕外。
WIN_W, WIN_H = 1818, 1144          # 默认窗口（像素）
WIN_X, WIN_Y = 370, 140

SCREEN_MODES = ("game", "primary", "secondary")
GAME_HINTS = ("endfield", "终末地", "arknights")
_OWN_TITLES = ("Endfield RotPilot",)
_BROWSER_PID = [0]

_BROWSER_EXES = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe",
                 "firefox.exe", "opera.exe")
# 这些进程永远不可能是游戏（窗口标题里带 endfield 的文件夹窗口会被误判，必须排掉）
_NOT_GAME_EXES = _BROWSER_EXES + (
    "explorer.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "systemsettings.exe", "taskmgr.exe",
    "python.exe", "pythonw.exe", "devenv.exe", "code.exe", "notepad.exe", "cmd.exe",
    "windowsterminal.exe", "conhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe")
_NOT_GAME_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
                     "CabinetWClass", "ExploreWClass", "ApplicationFrameWindow",
                     "Windows.UI.Core.CoreWindow", "TaskManagerWindow", "MultitaskingViewFrame",
                     "ForegroundStaging")
_MON_CACHE = {"t": 0.0, "list": []}


def _dpi_aware():
    """按显示器分别感知 DPI：坐标/尺寸都是物理像素，跨屏缩放不同也不会漂。"""
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_dpi_aware()


class _MONINFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT), ("rcNormalPosition", wintypes.RECT)]


def enum_monitors(force=False):
    """[{name, rect, work, primary, dpi, scale}]；主屏排最前，带 1.2 秒缓存。"""
    now = time.time()
    if not force and _MON_CACHE["list"] and now - _MON_CACHE["t"] < 1.2:
        return _MON_CACHE["list"]
    out = []
    try:
        u = ctypes.windll.user32
        u.EnumDisplayMonitors.restype = wintypes.BOOL

        def _cb(h, hdc, rc, lp):
            mi = _MONINFOEX()
            mi.cbSize = ctypes.sizeof(_MONINFOEX)
            if not u.GetMonitorInfoW(h, ctypes.byref(mi)):
                return True
            dpi = 96
            try:
                dx, dy = wintypes.UINT(), wintypes.UINT()
                ctypes.windll.shcore.GetDpiForMonitor(h, 0, ctypes.byref(dx), ctypes.byref(dy))
                if dx.value:
                    dpi = int(dx.value)
            except Exception:
                pass
            r, wk = mi.rcMonitor, mi.rcWork
            out.append({
                "name": mi.szDevice,
                "rect": [r.left, r.top, r.right, r.bottom],
                "work": [wk.left, wk.top, wk.right, wk.bottom],
                "primary": bool(mi.dwFlags & 1),
                "dpi": dpi,
                "scale": round(dpi / 96.0, 2),
            })
            return True

        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)(_cb)
        u.EnumDisplayMonitors(None, None, proc, 0)
    except Exception as e:
        log("enum_monitors fail %s" % e)
    if not out:
        out = [{"name": "\\\\.\\DISPLAY1", "rect": [0, 0, 1920, 1080],
                "work": [0, 0, 1920, 1040], "primary": True, "dpi": 96, "scale": 1.0}]
    out.sort(key=lambda m: (0 if m["primary"] else 1, m["rect"][0], m["rect"][1]))
    _MON_CACHE["t"], _MON_CACHE["list"] = now, out
    return out


def _mon_at(x, y, mons=None):
    mons = mons or enum_monitors()
    for i, m in enumerate(mons):
        l, t, r, b = m["rect"]
        if l <= x < r and t <= y < b:
            return i
    return 0 if mons else -1


def _main_windows(title="Endfield RotPilot"):
    """所有正常的「主窗口」：可见、没最小化、尺寸正常（排除 Edge 那个 314x50 的占位窗口）。"""
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, lp):
        try:
            b = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, b, 256)
            if b.value != title or not u.IsWindowVisible(h) or u.IsIconic(h):
                return True
            r = wintypes.RECT()
            u.GetWindowRect(wintypes.HWND(h), ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w >= 400 and hh >= 300 and r.left > -30000:
                out.append(int(h))
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def _find_main_hwnd(title="Endfield RotPilot"):
    try:
        ws = _main_windows(title)
        if ws:
            return ws[0]
        u = ctypes.windll.user32
        u.FindWindowW.restype = wintypes.HWND
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        return u.FindWindowW(None, title) or 0
    except Exception:
        return 0


def _mon_of_window(hwnd, mons=None):
    try:
        u = ctypes.windll.user32
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        hmon = u.MonitorFromWindow(hwnd, 2)      # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return -1
        mi = _MONINFOEX()
        mi.cbSize = ctypes.sizeof(_MONINFOEX)
        if not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return -1
        mons = mons or enum_monitors()
        for i, m in enumerate(mons):
            if m["name"] == mi.szDevice:
                return i
        cx = (mi.rcMonitor.left + mi.rcMonitor.right) // 2
        cy = (mi.rcMonitor.top + mi.rcMonitor.bottom) // 2
        return _mon_at(cx, cy, mons)
    except Exception:
        return -1


def _proc_exe(hwnd):
    try:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)      # QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


def _win_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def game_window():
    """找游戏窗口。

    优先级：进程名像游戏 > 标题像游戏 > 最大的普通窗口（猜的）。
    浏览器 / 资源管理器 / 系统壳窗口一律排除——否则「EndfieldRotPilot - 文件资源管理器」
    这种窗口也会被当成游戏（踩过）。
    """
    u = ctypes.windll.user32
    best = {"hard": [0, 0, "", ""], "mid": [0, 0, "", ""], "soft": [0, 0, "", ""]}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, lp):
        try:
            if not u.IsWindowVisible(h) or u.IsIconic(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 2)
            u.GetWindowTextW(h, buf, n + 2)
            title = buf.value
            if title in _OWN_TITLES:
                return True
            cls = _win_class(h)
            if cls in _NOT_GAME_CLASSES:
                return True
            r = wintypes.RECT()
            if not u.GetWindowRect(h, ctypes.byref(r)):
                return True
            w, hh = r.right - r.left, r.bottom - r.top
            if w < 640 or hh < 360:
                return True
            exe = _proc_exe(h)
            if not exe or exe in _NOT_GAME_EXES:
                return True
            area = w * hh
            low_e, low_t = exe.lower(), title.lower()
            if any(k in low_e for k in GAME_HINTS):
                if area > best["hard"][1]:
                    best["hard"] = [h, area, title, exe]
            elif any(k in low_t for k in GAME_HINTS):
                if area > best["mid"][1]:
                    best["mid"] = [h, area, title, exe]
            elif area > best["soft"][1]:
                best["soft"] = [h, area, title, exe]
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_cb, 0)
    except Exception:
        pass
    for key, sure in (("hard", True), ("mid", True), ("soft", False)):
        v = best[key]
        if v[0]:
            return {"hwnd": v[0], "area": v[1], "title": v[2], "exe": v[3], "sure": sure}
    return {"hwnd": 0, "area": 0, "title": "", "exe": "", "sure": False}


def pick_screen(cfg=None, mons=None):
    """目标显示器下标：主屏 / 副屏 / 跟随游戏（找不到游戏就沿用上次那块）。"""
    cfg = cfg or load_cfg()
    mons = mons or enum_monitors()
    if len(mons) <= 1:
        return 0
    mode = str(cfg.get("screenMode") or "game").lower()
    if mode not in SCREEN_MODES:
        mode = "game"
    if mode == "primary":
        for i, m in enumerate(mons):
            if m["primary"]:
                return i
        return 0
    if mode == "secondary":
        for i, m in enumerate(mons):
            if not m["primary"]:
                return i
        return 0
    # 只信“确定像游戏”的窗口；只是“最大窗口”的猜测不作为摆位依据
    # （否则桌面随便一个大窗口就能把辅助窗口拽到主屏去）
    g = game_window()
    if g["hwnd"] and g.get("sure", True):
        i = _mon_of_window(g["hwnd"], mons)
        if i >= 0:
            return i
    last = cfg.get("winMonitor") or ""
    for i, m in enumerate(mons):
        if m["name"] == last:
            return i
    for i, m in enumerate(mons):
        if m["primary"]:
            return i
    return 0


def _default_rect(mon):
    l, t, r, b = mon["work"]
    w = min(WIN_W, max(900, r - l - 40))
    h = min(WIN_H, max(560, b - t - 60))
    x = l + max(0, (r - l - w) // 2)
    y = t + max(16, (b - t - h) // 3)
    return [x, y, w, h]


def clamp_rect(rect, mon):
    """把矩形塞进这块屏的工作区（防止缩放不同/换了布局后跑到屏外）。"""
    l, t, r, b = mon["work"]
    w = max(640, min(int(rect[2]), max(640, r - l)))
    h = max(480, min(int(rect[3]), max(480, b - t)))
    x = max(l, min(int(rect[0]), r - w))
    y = max(t, min(int(rect[1]), b - h))
    return [x, y, w, h]


def main_window_rect(cfg=None, mons=None):
    """主窗口该摆哪：优先记住的位置（必须同一块屏），否则按目标屏算个居中位置。"""
    cfg = cfg or load_cfg()
    mons = mons or enum_monitors()
    i = pick_screen(cfg, mons)
    mon = mons[i]
    saved = cfg.get("winRect")
    same = (cfg.get("winMonitor") or "") == mon["name"]
    rect = None
    if isinstance(saved, dict) and all(k in saved for k in ("x", "y", "w", "h")) and same:
        try:
            rect = [int(saved["x"]), int(saved["y"]), int(saved["w"]), int(saved["h"])]
        except Exception:
            rect = None
    return clamp_rect(rect or _default_rect(mon), mon)


def _move_hwnd(hwnd, rect, no_activate=True, maximize=None):
    """把窗口摆到 rect（物理像素）。

    maximize:
        None  = 保持现在的最大化状态（用户最大化过就别给人家取消）
        True  = 摆成最大化（挂到 rect 所在那块屏）
        False = 摆成普通窗口

    踩过的坑：以前只要发现窗口是最大化就用 showCmd=1 强行取消，结果 Edge 一恢复最大化
    我们就再取消一次，来回抢 —— 表现就是「窗口没法全屏」。
    """
    if os.environ.get("ROTPILOT_NO_WINDOW_MOVE") == "1":
        log("skip window move (test): %s" % (rect,))
        return True
    try:
        u = ctypes.windll.user32
        h = wintypes.HWND(hwnd)
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
        u.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]

        zoomed = bool(u.IsZoomed(h))
        want_max = zoomed if maximize is None else bool(maximize)

        if want_max:
            # 最大化状态也要“搬家”：改 rcNormalPosition，再以 SW_SHOWMAXIMIZED 放回去
            wp = _WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if u.GetWindowPlacement(h, ctypes.byref(wp)):
                wp.showCmd = 3 if not zoomed else 0     # 3=SW_SHOWMAXIMIZED；0=不改
                wp.rcNormalPosition = wintypes.RECT(int(rect[0]), int(rect[1]),
                                                    int(rect[0]) + int(rect[2]),
                                                    int(rect[1]) + int(rect[3]))
                u.SetWindowPlacement(h, ctypes.byref(wp))
                log("place maximized -> %s" % (rect,))
            return True

        if zoomed:
            wp = _WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if u.GetWindowPlacement(h, ctypes.byref(wp)):
                wp.showCmd = 1                           # 取消最大化
                wp.rcNormalPosition = wintypes.RECT(int(rect[0]), int(rect[1]),
                                                    int(rect[0]) + int(rect[2]),
                                                    int(rect[1]) + int(rect[3]))
                u.SetWindowPlacement(h, ctypes.byref(wp))
                log("un-maximize -> %s" % (rect,))
        flags = 0x0004 | (0x0010 if no_activate else 0)   # SWP_NOZORDER (+SWP_NOACTIVATE)
        u.SetWindowPos(h, None, int(rect[0]), int(rect[1]),
                       int(rect[2]), int(rect[3]), flags)
        return True
    except Exception as e:
        log("move window fail %s" % e)
        return False


def apply_screen(mode=None, move_overlay=True):
    """设置里改了「显示屏幕」：存起来，并立刻把主窗口（和运行条）挪过去。"""
    cfg = load_cfg()
    if mode:
        m = str(mode).lower()
        cfg["screenMode"] = m if m in SCREEN_MODES else "game"
    save_cfg(cfg)
    mons = enum_monitors(force=True)
    i = pick_screen(cfg, mons)
    rect = main_window_rect(cfg, mons)
    _PLACED["mon"] = mons[i]["name"] if 0 <= i < len(mons) else ""
    hwnd = _find_main_hwnd()
    if hwnd:
        # 放到后台线程里挪：SetWindowPos 碰到没响应的窗口会卡住，别把界面请求一起拖住
        threading.Thread(target=_move_hwnd, args=(hwnd, rect), daemon=True).start()
        log("apply_screen -> %s #%d %s" % (cfg.get("screenMode"), i, rect))
    if move_overlay and _overlay and getattr(_overlay, "running", False):
        try:
            _overlay.set_screen(i)
        except Exception as e:
            log("overlay move fail %s" % e)
    return {"ok": True, "mode": cfg.get("screenMode"), "screen": i, "rect": rect,
            "monitors": len(mons)}


_RECT_TRACK = {"saved": None, "max": False, "pend": None, "pendmax": None, "n": 0}


def _track_main_rect():
    """每 2 秒看一下主窗口的位置/大小/是否最大化，稳定两拍就写进配置。"""
    cfg0 = load_cfg()
    r0 = cfg0.get("winRect")
    if isinstance(r0, dict) and all(k in r0 for k in ("x", "y", "w", "h")):
        _RECT_TRACK["saved"] = [int(r0["x"]), int(r0["y"]), int(r0["w"]), int(r0["h"])]
    _RECT_TRACK["max"] = bool(cfg0.get("winMax"))
    while True:
        time.sleep(2.0)
        try:
            hwnd = _find_main_hwnd()
            if not hwnd:
                continue
            if not _SETTLED[0]:
                continue          # 启动摆位还没稳定，别把中间态记下来
            u = ctypes.windll.user32
            u.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
            u.IsZoomed.argtypes = [wintypes.HWND]
            wp = _WINDOWPLACEMENT()
            wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
            if not u.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
                continue
            zoomed = bool(u.IsZoomed(wintypes.HWND(hwnd)))
            rc = wp.rcNormalPosition          # 最大化了也拿到“还原后”的大小
            rect = [rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top]
            if rect[2] < 400 or rect[3] < 300:
                continue
            # 只变了“最大化/还原”状态（用户把窗口拖下来或点最大化）→ 立刻记住，别等两拍
            if rect == _RECT_TRACK["saved"] and zoomed != _RECT_TRACK.get("max"):
                try:
                    cfg_m = load_cfg()
                    cfg_m["winMax"] = zoomed
                    save_cfg(cfg_m)
                    _RECT_TRACK["max"] = zoomed
                    _RECT_TRACK["pend"] = None
                    _RECT_TRACK["pendmax"] = None
                    _RECT_TRACK["n"] = 0
                    log("window max state -> %s" % zoomed)
                except Exception:
                    pass
                continue
            same = (rect == _RECT_TRACK["saved"]) and (zoomed == _RECT_TRACK.get("max"))
            if same:
                _RECT_TRACK["pend"] = None
                continue
            if rect == _RECT_TRACK["pend"] and zoomed == _RECT_TRACK.get("pendmax"):
                _RECT_TRACK["n"] += 1
            else:
                _RECT_TRACK["pend"] = rect
                _RECT_TRACK["pendmax"] = zoomed
                _RECT_TRACK["n"] = 1
                continue
            if _RECT_TRACK["n"] < 2:
                continue
            mons = enum_monitors(force=True)
            i = _mon_at(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2, mons)

            # 「跟随游戏」：游戏换屏了，而且主窗口还在我们上次摆的那块屏上 → 一起挪过去
            try:
                cfg_now = load_cfg()
                if str(cfg_now.get("screenMode") or "game").lower() == "game" and _SETTLED[0]:
                    g = game_window()
                    gi = _mon_of_window(g["hwnd"], mons) if (g["hwnd"] and g.get("sure")) else -1
                    here = mons[i]["name"] if 0 <= i < len(mons) else ""
                    if gi >= 0 and here and here == _PLACED["mon"] and gi != i:
                        tgt = clamp_rect(rect, mons[gi])
                        _move_hwnd(hwnd, tgt)          # maximize=None：现在是最大化就保持
                        _PLACED["mon"] = mons[gi]["name"]
                        log("game on screen #%d -> main window follows %s" % (gi, tgt))
                        continue
            except Exception:
                pass
            cfg = load_cfg()
            cfg["winRect"] = {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]}
            cfg["winMonitor"] = mons[i]["name"] if 0 <= i < len(mons) else ""
            cfg["winMax"] = zoomed
            save_cfg(cfg)
            _RECT_TRACK["saved"] = rect
            _RECT_TRACK["max"] = zoomed
            _RECT_TRACK["pend"] = None
            _RECT_TRACK["pendmax"] = None
            _RECT_TRACK["n"] = 0
            log("window rect saved %s (%s, max=%s)" % (rect, cfg.get("winMonitor"), zoomed))
        except Exception:
            pass


_WANT_RECT = [None]
# 我们最后把主窗口摆在哪块屏；用户自己拖到别的屏后，跟随逻辑就不再多管闲事
_PLACED = {"mon": ""}


def _want_rect():
    if _WANT_RECT[0] is None:
        try:
            _WANT_RECT[0] = main_window_rect()
        except Exception:
            _WANT_RECT[0] = [WIN_X, WIN_Y, WIN_W, WIN_H]
    return _WANT_RECT[0]




def _set_window_icon(hwnd):
    """给 Edge 窗口换上自己的图标（任务栏/标题栏都清楚）。"""
    import ctypes
    from ctypes import wintypes
    ico = os.path.join(BASE, "ui", "favicon.ico")
    if not os.path.exists(ico):
        return
    u = ctypes.windll.user32
    u.LoadImageW.restype = wintypes.HANDLE
    LR_LOADFROMFILE, IMAGE_ICON = 0x0010, 1
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    try:
        hbig = u.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        hsmall = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hbig:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hbig)
            u.SetClassLongPtrW(hwnd, -14, hbig)      # GCLP_HICON
        if hsmall:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hsmall)
            u.SetClassLongPtrW(hwnd, -34, hsmall)    # GCLP_HICONSM
        log("window icon set")
    except Exception as e:
        log("icon fail %s" % e)


_SETTLED = [False]       # 窗口摆位是否已经稳定（稳定前不要记位置，免得记到中间态）


def _covers_monitor(rect, tol=4):
    """rect 是不是正好铺满某块屏（Edge 的 F11 全屏就长这样，没有标题栏）。"""
    try:
        for m in enum_monitors():
            x, y, r, b = m["rect"]
            if (abs(rect[0] - x) <= tol and abs(rect[1] - y) <= tol
                    and abs((rect[0] + rect[2]) - r) <= tol
                    and abs((rect[1] + rect[3]) - b) <= tol):
                return True
    except Exception:
        pass
    return False


def _force_window_size(title="Endfield RotPilot", timeout=13.0, hold=2.5):
    """把主窗口摆到目标位置。

    Edge 打开后会按自己记的状态再调（实测 1.6 秒摆一次、8.8 秒又直接最大化），
    所以要盯一小会儿。但**用户/Edge 想最大化时必须让路**：
      · 配置里 winMax=True  → 直接摆成最大化并结束；
      · 中途发现窗口被最大化了 → 认定“就是要最大化”，记下来、立刻停止干预。
    """
    import ctypes
    from ctypes import wintypes
    _dpi_aware()
    rect = _want_rect()
    try:
        want_max = bool(load_cfg().get("winMax"))
    except Exception:
        want_max = False
    u = ctypes.windll.user32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.IsZoomed.argtypes = [wintypes.HWND]
    t0 = time.time()
    good_since = None
    icon_done = False
    drift = 0

    def _accept_max(hwnd):
        """用户/Edge 要最大化：记下来，永不再改。"""
        try:
            cfg = load_cfg()
            cfg["winMax"] = True
            save_cfg(cfg)
        except Exception:
            pass
        _SETTLED[0] = True
        log("window is maximized -> keep it (no more forcing)")
        if not icon_done:
            _set_window_icon(hwnd)

    if want_max:
        # 上次关的时候是最大化的：只摆一次最大化，摆完就彻底不管。
        # （以前这里盯 9 秒、掉了就再补 —— 用户手动把窗口拖下来又会被推回最大化。）
        for _ in range(12):                     # 最多等 3.6 秒把窗口等出来
            hwnd = _find_main_hwnd(title)
            if hwnd:
                if not icon_done:
                    _set_window_icon(hwnd)
                    icon_done = True
                _move_hwnd(hwnd, rect, maximize=True)
                _SETTLED[0] = True
                log("window opened maximized (one shot) %s" % (rect,))
                return True
            time.sleep(0.3)
        _SETTLED[0] = True
        return False

    max_streak = 0
    while time.time() - t0 < timeout:
        hwnd = _find_main_hwnd(title)
        if hwnd:
            if u.IsZoomed(wintypes.HWND(hwnd)):
                # 稳定最大化 1.5 秒才算“人家真要最大化”；
                # Edge 启动瞬间那一下（1 秒左右）是它自己在恢复，别被它带跑。
                max_streak += 1
                if max_streak >= 5:
                    _accept_max(hwnd)
                    return True
                time.sleep(0.3)
                continue
            max_streak = 0
            r = wintypes.RECT()
            cur = None
            if u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
                cur = [r.left, r.top, r.right - r.left, r.bottom - r.top]
            if cur and cur != rect and _covers_monitor(cur):
                # F11 全屏：一样是“人家要的”，立刻收手
                _SETTLED[0] = True
                log("window is fullscreen (F11) -> keep it")
                if not icon_done:
                    _set_window_icon(hwnd)
                return True
            if cur == rect:
                if good_since is None:
                    good_since = time.time()
                    log("window at %s (watching)" % (rect,))
                elif time.time() - good_since >= hold:
                    _SETTLED[0] = True
                    try:
                        cfg = load_cfg()
                        if cfg.get("winMax"):
                            cfg["winMax"] = False
                            save_cfg(cfg)
                    except Exception:
                        pass
                    log("window settled %s" % (rect,))
                    return True
            else:
                if good_since is not None:
                    drift += 1
                    log("window drifted to %s, fixing (%d)" % (cur, drift))
                good_since = None
                _move_hwnd(hwnd, rect, maximize=False)
                if not icon_done:
                    _set_window_icon(hwnd)
                    icon_done = True
                    log("window fixed %sx%s @%s,%s" % (rect[2], rect[3], rect[0], rect[1]))
        time.sleep(0.3)
    _SETTLED[0] = True
    log("window place timeout, keep current")
    return False


def _find_browser():
    """找一个 Chromium 内核浏览器（支持 --app 独立窗口）。"""
    import winreg
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c, os.path.basename(c)

    # 再查注册表里登记的路径
    for name in ("msedge.exe", "chrome.exe", "brave.exe"):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in ("SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name,
                        "SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        path = winreg.QueryValue(k, None)
                        if path and os.path.exists(path):
                            return path, name
                except Exception:
                    pass
    return None, None


def close_app_window():
    """把我们开的那个 --app 窗口关掉（发 WM_CLOSE，不会误杀其他 Edge 窗口）。"""
    import subprocess
    flags = 0x08000000
    closed = 0
    try:
        u = ctypes.windll.user32
        hwnds = []
        hwnds += _main_windows("Endfield RotPilot")
        hwnds += _main_windows("Endfield RotPilot 旧版测试")
        for h in set(hwnds):
            if u.PostMessageW(wintypes.HWND(h), 0x0010, 0, 0):   # WM_CLOSE
                closed += 1
        log("browser WM_CLOSE sent to %d window(s)" % closed)
    except Exception as e:
        log("browser close fail %s" % e)
    if closed:
        return
    # 兜底：按 pid / 命令行找
    pid = _BROWSER_PID[0]
    try:
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           creationflags=flags, capture_output=True)
        else:
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
                  "'*--app=http://127.0.0.1:" + str(PORT) + "*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           creationflags=flags, capture_output=True)
    except Exception as e:
        log("browser kill fail %s" % e)


def open_app_window(url):
    """用 Chromium 浏览器的 --app 模式打开，看起来像原生窗口。"""
    if os.environ.get("ROTPILOT_NO_BROWSER") == "1":
        log("browser skipped (ROTPILOT_NO_BROWSER)")
        return True
    import subprocess
    exe, name = _find_browser()
    if exe:
        try:
            rect = _want_rect()
            _p = subprocess.Popen([exe, f"--app={url}",
                              f"--window-size={rect[2]},{rect[3]}",
                              f"--window-position={rect[0]},{rect[1]}",
                              "--no-first-run", "--no-default-browser-check",
                              "--disable-features=Translate,msEdgeTranslate"],
                             close_fds=True)
            _BROWSER_PID[0] = _p.pid
            log("browser: %s (pid=%d)" % (exe, _p.pid))
            threading.Thread(target=_force_window_size, daemon=True).start()
            return True
        except Exception as e:
            log("browser launch fail %s" % e)

    # 实在没有 Chromium：用系统默认浏览器（会是普通标签页）
    log("no chromium browser, fallback to default browser")
    webbrowser.open(url)
    return False





_ROSTER_MTIME = [0.0]


def _refresh_roster_if_changed():
    """roster.json 变了（比如在资源管理器里删了干员）就重载。"""
    try:
        path = os.path.join(CORE_DIR, "operators", "roster.json")
        m = os.path.getmtime(path)
        if m != _ROSTER_MTIME[0]:
            _ROSTER_MTIME[0] = m
            _reload_core()
            log("roster changed, core reloaded")
    except Exception:
        pass


def _reload_core():
    """名单变了以后重新加载 core，让 OPS 立刻包含新干员。"""
    import importlib
    importlib.reload(core)
    try:
        import make_cards
        importlib.reload(make_cards)
    except Exception:
        pass


def import_operator_zip(data, filename=""):
    """把干员包（zip）装进程序。返回 (ok, 结果/错误)。"""
    import tempfile
    import zipfile

    if not data:
        return False, "空文件"
    log("import: start %d bytes" % len(data))

    tmpdir = tempfile.mkdtemp(prefix="op_")
    zpath = os.path.join(tmpdir, "p.zip")
    with open(zpath, "wb") as f:
        f.write(data)

    log("import: unzip")
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmpdir)
    except Exception as e:
        return False, f"压缩包打不开：{e}"

    # 找 operator.json（允许包一层目录）
    meta_path = None
    root = tmpdir
    for base, _dirs, files in os.walk(tmpdir):
        if "operator.json" in files:
            meta_path = os.path.join(base, "operator.json")
            root = base
            break
    if not meta_path:
        return False, "包里没有 operator.json"

    try:
        meta = json.load(open(meta_path, encoding="utf-8-sig"))
    except Exception as e:
        return False, f"operator.json 读不出来：{e}"

    name = (meta.get("name") or "").strip()
    element = (meta.get("element") or "").strip()
    cls = (meta.get("class") or meta.get("cls") or "").strip()
    if not name:
        return False, "operator.json 缺 name"

    opdir = os.path.join(CORE_DIR, "operators", name)
    os.makedirs(opdir, exist_ok=True)
    for kind in ("avatar", "skill", "link", "ult"):
        src = os.path.join(root, f"{kind}.png")
        if not os.path.exists(src):
            continue
        from PIL import Image
        Image.open(src).convert("RGBA").save(os.path.join(opdir, f"{kind}.png"))
    if element:
        open(os.path.join(opdir, "element.txt"), "w", encoding="utf-8").write(element)
    if cls:
        open(os.path.join(opdir, "class.txt"), "w", encoding="utf-8").write(cls)

    # 每个技能可以有自己的属性（有的干员技能属性与自身不同）
    els = meta.get("elements") if isinstance(meta.get("elements"), dict) else {}
    colors = {}
    for kind in ("skill", "link", "ult"):
        el_name = str(els.get(kind) or "").strip() or element
        col = core.ELEMENT_COLORS.get(el_name)
        if col:
            colors[kind] = "#%02X%02X%02X" % col
    if colors:
        with open(os.path.join(opdir, "colors.json"), "w", encoding="utf-8") as f:
            json.dump(colors, f, ensure_ascii=False, indent=2)

    # 名额
    rpath = os.path.join(CORE_DIR, "operators", "roster.json")
    roster = {}
    if os.path.exists(rpath):
        try:
            roster = json.load(open(rpath, encoding="utf-8-sig"))
        except Exception:
            roster = {}
    slot = None
    for k, v in roster.items():
        if v == name:
            slot = int(k)
            break
    if slot is None:
        for i in range(1, 61):
            if str(i) not in roster:
                slot = i
                break
        else:
            slot = max([int(k) for k in roster] or [0]) + 1
        roster[str(slot)] = name
        with open(rpath, "w", encoding="utf-8") as f:
            json.dump(roster, f, ensure_ascii=False, indent=2)

    log("import: reload core")
    _reload_core()
    log("import: reloaded")

    # 卡面 + 图标
    log("import: card")
    try:
        import make_cards
        make_cards.make_card(name).save(os.path.join(CORE_DIR, "cards", f"{name}.png"))
    except Exception as e:
        log("card fail %s" % e)
    log("import: icons")
    # 先删掉旧的缓存图标，否则会拿到上次的颜色
    gen = os.path.join(CORE_DIR, "gen")
    if os.path.isdir(gen):
        for f in os.listdir(gen):
            if name in f and f.endswith(".png"):
                try:
                    os.remove(os.path.join(gen, f))
                except Exception:
                    pass
    col = core.ELEMENT_COLORS.get(element, (110, 114, 120))
    for kind in ("skill", "link", "ult"):
        try:
            core.icon(kind, col, "", name, badge=True)
        except Exception as e:
            log("icon fail %s %s" % (kind, e))

    log("operator imported: %s (slot %s)" % (name, slot))
    return True, {"name": name, "slot": slot, "element": element, "class": cls}


def find_running():
    """并行探测是否已有实例在跑（串行扫 20 个端口太慢）。"""
    import urllib.request
    found = []

    def probe(port):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/whoami", timeout=0.12) as r:
                data = json.loads(r.read().decode("utf-8"))
                if data.get("app") == "rotpilot":
                    found.append(port)
        except Exception:
            pass

    threads = [threading.Thread(target=probe, args=(p,), daemon=True)
               for p in range(8777, 8790)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(0.25)
    return min(found) if found else None



def _watch_browser_window(title="Endfield RotPilot"):
    """已停用：之前会在窗口标题临时变化时误判，把程序本身关掉。"""
    return

def _restart_self():
    """重启：交给 启动.bat 重新拉起（有黑框、拿管理员），再退出自己。"""
    try:
        import subprocess
        bat = os.path.join(BASE, "启动.bat")
        log("restart via bat")
        if os.path.exists(bat):
            subprocess.Popen(["cmd", "/c", "start", "", bat],
                             cwd=BASE, close_fds=True)
        else:
            subprocess.Popen([sys.executable, os.path.join(BASE, "app_web.py")],
                             cwd=BASE, close_fds=True)
    except Exception as e:
        log("restart fail %s" % e)
    try:
        time.sleep(0.5)
    except Exception:
        pass
    os._exit(0)


def _is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_as_admin():
    """不是管理员就重新以管理员身份拉起自己（读游戏按键必须提权）。"""
    import ctypes
    try:
        script = os.path.abspath(__file__)
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}"', BASE, 1)
        return bool(r and r > 32)
    except Exception as e:
        log("elevate fail %s" % e)
        return False


def pick_port(start=8777, tries=20):
    """端口被占用就往后找一个。"""
    import socket
    for i in range(tries):
        port = start + i
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            try:
                s.close()
            except Exception:
                pass
    return start


def main():
    global PORT
    _t0 = time.time()

    if not _is_admin() and os.environ.get("ROTPILOT_NO_ELEVATE") != "1":
        log("not admin, trying to elevate")
        if _relaunch_as_admin():
            return
        log("elevate refused, running without admin (game keys may not be captured)")

    log("admin: %s (t=%.2fs)" % (_is_admin(), time.time() - _t0))

    cfg = load_cfg()
    save_cfg(cfg)

    force_new = os.environ.get("ROTPILOT_FORCE_NEW") == "1"
    if os.environ.get("ROTPILOT_PORT"):
        try:
            PORT = int(os.environ["ROTPILOT_PORT"])
        except Exception:
            pass

    # 已经有一个在跑 → 直接打开它的窗口，不再起第二个
    running = None if force_new else find_running()
    if running:
        log("already running on %s, reuse" % running)
        open_app_window(f"http://127.0.0.1:{running}/")
        return

    PORT = pick_port(PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log("server ready (t=%.2fs)" % (time.time() - _t0))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("server on %d" % PORT)
    threading.Thread(target=_watchdog, daemon=True).start()   # 主窗口没了就自动收摊
    threading.Thread(target=_track_main_rect, daemon=True).start()   # 记住窗口位置/大小
    log("screen mode %s -> #%d" % (cfg.get("screenMode") or "game", pick_screen(cfg)))
    url = f"http://127.0.0.1:{PORT}/"
    open_app_window(url)
    print(f"RotPilot Web 已启动：{url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("app crashed:\n%s" % traceback.format_exc())
        raise
