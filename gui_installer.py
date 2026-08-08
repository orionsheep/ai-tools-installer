import os
import sys
import platform
import tarfile
import zipfile
import shutil
import subprocess
import threading
import locale
import webbrowser
import tkinter as tk
from tkinter import messagebox
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------
IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"
ARCH = platform.machine().lower()
IS_ARM = "arm" in ARCH or "aarch64" in ARCH

# Prevents a console window from flashing/popping up when this --windowed
# GUI app spawns npm.cmd or other console subprocesses on Windows.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if IS_WIN else 0


def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def add_to_path_win(target_dir):
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_ALL_ACCESS)
        current_path, _ = winreg.QueryValueEx(key, "PATH")
        if target_dir not in current_path:
            new_path = current_path + ";" + target_dir
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, new_path)
            subprocess.run(["setx", "PATH", new_path], shell=True, stdout=subprocess.DEVNULL,
                            creationflags=_NO_WINDOW_FLAGS)
    except Exception as e:
        print(f"Failed to update PATH: {e}")


def get_install_dirs():
    if IS_MAC:
        bin_dir = os.path.expanduser("~/.local/bin")
        app_dir = os.path.expanduser("~/.local/share/ai_tools_env")
    elif IS_WIN:
        bin_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "ai_tools_bin")
        app_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "ai_tools_env")
    else:
        raise Exception("Unsupported OS")

    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(app_dir, exist_ok=True)

    if IS_MAC:
        shell_rc = os.path.expanduser("~/.zshrc")
        if os.path.exists(shell_rc):
            with open(shell_rc, "r") as f:
                content = f.read()
            if "export PATH=\"$HOME/.local/bin:$PATH\"" not in content:
                with open(shell_rc, "a") as f:
                    f.write('\nexport PATH="$HOME/.local/bin:$PATH"\n')

    elif IS_WIN:
        add_to_path_win(bin_dir)

    return bin_dir, app_dir


def install_codex():
    bin_dir, _ = get_install_dirs()
    if IS_MAC:
        filename = "codex-mac-arm64.tar.gz" if IS_ARM else "codex-mac-x64.tar.gz"
        archive_path = get_resource_path(f"payload/{filename}")
        if not os.path.exists(archive_path):
            raise Exception(f"macOS Codex payload not found: {filename}")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=bin_dir)
            for member in tar.getmembers():
                if member.isfile() and "codex" in member.name:
                    extracted_path = os.path.join(bin_dir, member.name)
                    final_path = os.path.join(bin_dir, "codex")
                    if extracted_path != final_path:
                        shutil.move(extracted_path, final_path)
                    os.chmod(final_path, 0o755)
    elif IS_WIN:
        archive_path = get_resource_path("payload/codex-win-x64.zip")
        if not os.path.exists(archive_path):
            raise Exception("Windows Codex payload not found.")
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(bin_dir)
            for root, dirs, files in os.walk(bin_dir):
                for file in files:
                    if "codex.exe" in file:
                        extracted_path = os.path.join(root, file)
                        final_path = os.path.join(bin_dir, "codex.exe")
                        if extracted_path != final_path:
                            shutil.move(extracted_path, final_path)


# ---------------------------------------------------------------------------
# Shared helpers for the Node.js-based tools (Claude Code, Gemini, Kimi, Lark)
# ---------------------------------------------------------------------------
def _ensure_node(app_dir):
    """Extract the bundled portable Node.js runtime once. Safe to call repeatedly."""
    node_dir = os.path.join(app_dir, "node")
    if os.path.exists(node_dir):
        return node_dir

    if IS_MAC:
        filename = "node-mac-arm64.tar.gz" if IS_ARM else "node-mac-x64.tar.gz"
        archive_path = get_resource_path(f"payload/{filename}")
        if not os.path.exists(archive_path):
            raise Exception(f"macOS Node payload not found: {filename}")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=app_dir)
            extracted_folder = [m.name for m in tar.getmembers() if m.isdir()][0].split('/')[0]
        shutil.move(os.path.join(app_dir, extracted_folder), node_dir)
    elif IS_WIN:
        archive_path = get_resource_path("payload/node-win-x64.zip")
        if not os.path.exists(archive_path):
            raise Exception("Windows Node payload not found.")
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(path=app_dir)
            extracted_folder = zip_ref.namelist()[0].split('/')[0]
        shutil.move(os.path.join(app_dir, extracted_folder), node_dir)

    return node_dir


def _npm_bin(node_dir):
    if IS_MAC:
        return os.path.join(node_dir, "bin", "npm")
    return os.path.join(node_dir, "npm.cmd")


def _run_npm(args, env):
    """Run an npm command with output captured (never inherited) so no console
    window flashes on Windows, and so a failure's stderr tail is actually visible
    in the error dialog instead of a bare 'non-zero exit status'."""
    try:
        subprocess.run(args, check=True, env=env, capture_output=True, text=True,
                        creationflags=_NO_WINDOW_FLAGS)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or e.stdout or "").strip().splitlines()[-15:]
        raise Exception("npm failed:\n" + "\n".join(tail)) from e


def _find_payload_tgz(prefix):
    payload_dir = get_resource_path("payload")
    for f in os.listdir(payload_dir):
        if f.startswith(prefix) and f.endswith(".tgz"):
            return os.path.join(payload_dir, f)
    return None


def _expose_shim(bin_dir, node_dir, shim_name):
    """Expose a shim created inside node_dir by `npm install -g` via bin_dir (on PATH)."""
    if IS_MAC:
        node_shim = os.path.join(node_dir, "bin", shim_name)
        target_link = os.path.join(bin_dir, shim_name)
        if os.path.exists(target_link) or os.path.islink(target_link):
            os.remove(target_link)
        os.symlink(node_shim, target_link)
    elif IS_WIN:
        node_shim = os.path.join(node_dir, shim_name + ".cmd")
        target_bat = os.path.join(bin_dir, shim_name + ".cmd")
        with open(target_bat, "w") as f:
            f.write(f'@echo off\n"{node_shim}" %*')


def install_claude_code():
    """Claude Code v2's npm package is only a thin wrapper — the real binary
    lives in a platform-specific optionalDependency
    (@anthropic-ai/claude-code-<os>-<arch>) that npm downloads from the
    registry at install time, which breaks fully-offline installs. We bundle
    that platform package in payload/ instead and extract its standalone
    binary directly: no Node.js, no npm, no network."""
    bin_dir, app_dir = get_install_dirs()

    if IS_MAC:
        prefix = "anthropic-ai-claude-code-darwin-arm64" if IS_ARM else "anthropic-ai-claude-code-darwin-x64"
        bin_name = "claude"
    elif IS_WIN:
        prefix = "anthropic-ai-claude-code-win32-x64"
        bin_name = "claude.exe"
    else:
        raise Exception("Unsupported OS")

    tgz = _find_payload_tgz(prefix)
    if not tgz:
        raise Exception("Claude Code native package not found in payload.")

    target = os.path.join(bin_dir, bin_name)
    with tarfile.open(tgz, "r:gz") as tf:
        src = tf.extractfile(tf.getmember("package/" + bin_name))
        with open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    if not IS_WIN:
        os.chmod(target, 0o755)


def install_gemini():
    bin_dir, app_dir = get_install_dirs()
    node_dir = _ensure_node(app_dir)
    npm_bin = _npm_bin(node_dir)

    tgz = _find_payload_tgz("google-gemini-cli")
    if not tgz:
        raise Exception("Gemini CLI npm package not found in payload.")

    _run_npm([npm_bin, "install", "-g", tgz], os.environ.copy())
    _expose_shim(bin_dir, node_dir, "gemini")


def install_kimi():
    bin_dir, app_dir = get_install_dirs()
    node_dir = _ensure_node(app_dir)
    npm_bin = _npm_bin(node_dir)

    tgz = _find_payload_tgz("moonshot-ai-kimi-code")
    if not tgz:
        raise Exception("Kimi Code CLI npm package not found in payload.")

    _run_npm([npm_bin, "install", "-g", tgz], os.environ.copy())
    _expose_shim(bin_dir, node_dir, "kimi")


def install_feishu():
    """@larksuite/cli ships a Go binary. Its postinstall script downloads that binary
    from GitHub over the network, which would break an offline install — so we skip
    that script (--ignore-scripts) and place the binary (bundled in payload/, fetched
    during the CI build) into the exact path the package's own launcher expects."""
    bin_dir, app_dir = get_install_dirs()
    node_dir = _ensure_node(app_dir)
    npm_bin = _npm_bin(node_dir)

    tgz = _find_payload_tgz("larksuite-cli")
    if not tgz:
        raise Exception("Feishu (lark-cli) npm package not found in payload.")

    _run_npm([npm_bin, "install", "-g", "--ignore-scripts", tgz], os.environ.copy())

    if IS_MAC:
        pkg_bin_dir = os.path.join(node_dir, "lib", "node_modules", "@larksuite", "cli", "bin")
        filename = "lark-cli-mac-arm64.tar.gz" if IS_ARM else "lark-cli-mac-x64.tar.gz"
        archive_path = get_resource_path(f"payload/{filename}")
        if not os.path.exists(archive_path):
            raise Exception(f"macOS lark-cli payload not found: {filename}")
        os.makedirs(pkg_bin_dir, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=pkg_bin_dir)
        os.chmod(os.path.join(pkg_bin_dir, "lark-cli"), 0o755)
    elif IS_WIN:
        pkg_bin_dir = os.path.join(node_dir, "node_modules", "@larksuite", "cli", "bin")
        archive_path = get_resource_path("payload/lark-cli-win-x64.zip")
        if not os.path.exists(archive_path):
            raise Exception("Windows lark-cli payload not found.")
        os.makedirs(pkg_bin_dir, exist_ok=True)
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            zip_ref.extractall(path=pkg_bin_dir)

    _expose_shim(bin_dir, node_dir, "lark-cli")


# ---------------------------------------------------------------------------
# Tool registry — drives both the GUI rows and the install worker
# ---------------------------------------------------------------------------
TOOLS = [
    {"id": "codex", "icon": "C", "color": "#3B82C4", "install": install_codex},
    {"id": "claude", "icon": "A", "color": "#CC785C", "install": install_claude_code},
    {"id": "gemini", "icon": "G", "color": "#8B5CF6", "install": install_gemini},
    {"id": "kimi", "icon": "K", "color": "#0EA5A6", "install": install_kimi},
    {"id": "feishu", "icon": "L", "color": "#3370FF", "install": install_feishu},
]


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------
LANGS = ["en", "zh-Hans", "zh-Hant"]
LANG_LABELS = {"en": "EN", "zh-Hans": "简", "zh-Hant": "繁"}

STRINGS = {
    "en": {
        "app_title": "AI Tools Installer",
        "app_subtitle": "Set up 5 AI coding CLIs — fully offline",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI's coding agent for your terminal",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic's coding agent for your terminal",
        "gemini_title": "Gemini CLI",
        "gemini_desc": "Google's coding agent for your terminal",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI's coding agent for your terminal",
        "feishu_title": "Lark CLI",
        "feishu_desc": "Official CLI for Feishu/Lark AI agents",
        "install_button": "Install Now",
        "installing_button": "Installing…",
        "status_idle": "",
        "status_installing_codex": "Installing Codex CLI…",
        "status_installing_claude": "Installing Claude Code (Node.js)…",
        "status_installing_gemini": "Installing Gemini CLI (Node.js)…",
        "status_installing_kimi": "Installing Kimi Code CLI (Node.js)…",
        "status_installing_feishu": "Installing Lark CLI (Node.js)…",
        "status_done": "Installation complete",
        "status_failed": "Installation failed",
        "success_title": "Success",
        "success_body": "Installation successful!\n\nInstalled to:\n{path}\n\nRestart your terminal to use the tools you installed.",
        "error_title": "Error",
        "error_body": "Something went wrong:\n{error}",
        "footer_hint": "Uncheck a tool to skip installing it.",
    },
    "zh-Hans": {
        "app_title": "AI 工具安装向导",
        "app_subtitle": "离线安装 5 款 AI 编程 CLI",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI 出品的终端编程助手",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic 出品的终端编程助手",
        "gemini_title": "Gemini CLI",
        "gemini_desc": "Google 出品的终端编程助手",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI 出品的终端编程助手",
        "feishu_title": "飞书 CLI",
        "feishu_desc": "飞书官方 CLI，让 AI Agent 直接操作你的飞书",
        "install_button": "立即安装",
        "installing_button": "安装中…",
        "status_idle": "",
        "status_installing_codex": "正在安装 Codex CLI…",
        "status_installing_claude": "正在安装 Claude Code (Node.js)…",
        "status_installing_gemini": "正在安装 Gemini CLI (Node.js)…",
        "status_installing_kimi": "正在安装 Kimi Code CLI (Node.js)…",
        "status_installing_feishu": "正在安装飞书 CLI (Node.js)…",
        "status_done": "安装完成",
        "status_failed": "安装失败",
        "success_title": "安装成功",
        "success_body": "安装成功！\n\n已安装至：\n{path}\n\n请重启终端后使用已安装的工具。",
        "error_title": "出错了",
        "error_body": "安装过程中出现错误：\n{error}",
        "footer_hint": "取消勾选可跳过对应工具的安装。",
    },
    "zh-Hant": {
        "app_title": "AI 工具安裝精靈",
        "app_subtitle": "離線安裝 5 款 AI 程式設計 CLI",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI 推出的終端機程式設計助手",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic 推出的終端機程式設計助手",
        "gemini_title": "Gemini CLI",
        "gemini_desc": "Google 推出的終端機程式設計助手",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI 推出的終端機程式設計助手",
        "feishu_title": "飛書 CLI",
        "feishu_desc": "飛書官方 CLI，讓 AI Agent 直接操作你的飛書",
        "install_button": "立即安裝",
        "installing_button": "安裝中…",
        "status_idle": "",
        "status_installing_codex": "正在安裝 Codex CLI…",
        "status_installing_claude": "正在安裝 Claude Code (Node.js)…",
        "status_installing_gemini": "正在安裝 Gemini CLI (Node.js)…",
        "status_installing_kimi": "正在安裝 Kimi Code CLI (Node.js)…",
        "status_installing_feishu": "正在安裝飛書 CLI (Node.js)…",
        "status_done": "安裝完成",
        "status_failed": "安裝失敗",
        "success_title": "安裝成功",
        "success_body": "安裝成功！\n\n已安裝至：\n{path}\n\n請重新啟動終端機後使用已安裝的工具。",
        "error_title": "發生錯誤",
        "error_body": "安裝過程中發生錯誤：\n{error}",
        "footer_hint": "取消勾選可略過該工具的安裝。",
    },
}


def detect_language():
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:
        loc = ""
    if not loc:
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            loc = os.environ.get(var, "")
            if loc:
                break
    loc = loc.lower()
    if "zh" not in loc:
        return "en"
    if any(tag in loc for tag in ("tw", "hk", "mo", "hant")):
        return "zh-Hant"
    return "zh-Hans"


# ---------------------------------------------------------------------------
# Platform-aware design tokens
# ---------------------------------------------------------------------------
if IS_WIN:
    COLOR_BG = "#F3F3F3"
    COLOR_CARD = "#FFFFFF"
    COLOR_CARD_BORDER = "#E5E5E5"
    COLOR_SHADOW = "#E4E4E4"
    COLOR_TEXT = "#1A1A1A"
    COLOR_SUBTEXT = "#5C5C5C"
    COLOR_ACCENT = "#0067C0"
    COLOR_ACCENT_HOVER = "#005A9E"
    COLOR_TOGGLE_OFF = "#C7C7C7"
    COLOR_DIVIDER = "#EBEBEB"
    RADIUS_CARD = 8
    RADIUS_BTN = 6
    RADIUS_ICON = 8
    FONT_FAMILY = "Segoe UI"
    FONT_FAMILY_ZH = "Microsoft YaHei UI"
else:
    COLOR_BG = "#F5F5F7"
    COLOR_CARD = "#FFFFFF"
    COLOR_CARD_BORDER = "#E3E3E6"
    COLOR_SHADOW = "#E6E6EA"
    COLOR_TEXT = "#1D1D1F"
    COLOR_SUBTEXT = "#6E6E73"
    COLOR_ACCENT = "#007AFF"
    COLOR_ACCENT_HOVER = "#0066D6"
    COLOR_TOGGLE_OFF = "#D1D1D6"
    COLOR_DIVIDER = "#ECECEE"
    RADIUS_CARD = 16
    RADIUS_BTN = 10
    RADIUS_ICON = 12
    FONT_FAMILY = ".AppleSystemUIFont"
    FONT_FAMILY_ZH = "PingFang SC"

COLOR_SUCCESS = "#2E9B4E" if not IS_WIN else "#107C10"
COLOR_ERROR = "#D63A2E" if not IS_WIN else "#C42B1C"


def family_for(lang):
    if lang == "zh-Hant":
        return "PingFang TC" if IS_MAC else FONT_FAMILY_ZH
    if lang == "zh-Hans":
        return FONT_FAMILY_ZH
    return FONT_FAMILY


# ---------------------------------------------------------------------------
# Canvas drawing helpers
# ---------------------------------------------------------------------------
def rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def darken(hex_color, factor=0.85):
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r, g, b = int(r * factor), int(g * factor), int(b * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


class ToggleSwitch:
    def __init__(self, canvas, x, y, value=True, on_color=COLOR_ACCENT, off_color=COLOR_TOGGLE_OFF, command=None):
        self.canvas = canvas
        self.x, self.y = x, y
        self.w, self.h = 40, 22
        self.value = value
        self.on_color = on_color
        self.off_color = off_color
        self.command = command
        self.track_id = rounded_rect(canvas, x, y, x + self.w, y + self.h, self.h / 2,
                                      fill=self._track_color(), outline="")
        self.knob_id = canvas.create_oval(0, 0, 0, 0, fill="white", outline="")
        self._set_knob_pos(animate=False)
        for item in (self.track_id, self.knob_id):
            canvas.tag_bind(item, "<Button-1>", self._on_click)
            canvas.tag_bind(item, "<Enter>", lambda e: canvas.config(cursor="pointinghand" if IS_MAC else "hand2"))
            canvas.tag_bind(item, "<Leave>", lambda e: canvas.config(cursor=""))

    def _track_color(self):
        return self.on_color if self.value else self.off_color

    def _target_cx(self):
        pad = 2
        r = self.h / 2 - pad
        return (self.x + self.w - pad - r) if self.value else (self.x + pad + r)

    def _set_knob_pos(self, animate=True):
        pad = 2
        r = self.h / 2 - pad
        cy = self.y + self.h / 2
        target_cx = self._target_cx()
        if not animate:
            self.canvas.coords(self.knob_id, target_cx - r, cy - r, target_cx + r, cy + r)
            return
        coords = self.canvas.coords(self.knob_id)
        start_cx = (coords[0] + coords[2]) / 2 if coords else target_cx
        self._animate(target_cx, cy, r, start_cx, 0, 6)

    def _animate(self, target_cx, cy, r, start_cx, step, steps):
        if step > steps:
            self.canvas.coords(self.knob_id, target_cx - r, cy - r, target_cx + r, cy + r)
            return
        frac = step / steps
        cx = start_cx + (target_cx - start_cx) * frac
        self.canvas.coords(self.knob_id, cx - r, cy - r, cx + r, cy + r)
        self.canvas.after(9, lambda: self._animate(target_cx, cy, r, start_cx, step + 1, steps))

    def _on_click(self, event):
        self.set(not self.value)

    def set(self, value):
        self.value = value
        self.canvas.itemconfig(self.track_id, fill=self._track_color())
        self._set_knob_pos(animate=True)
        if self.command:
            self.command(self.value)

    def get(self):
        return self.value


class RoundedButton:
    def __init__(self, canvas, x, y, w, h, text, command, bg, fg="white", font=None, radius=RADIUS_BTN):
        self.canvas = canvas
        self.x, self.y, self.w, self.h = x, y, w, h
        self.command = command
        self.bg = bg
        self.hover_bg = darken(bg)
        self.disabled_bg = "#BFBFBF" if IS_WIN else "#C7C7CC"
        self.fg = fg
        self.enabled = True
        self.rect_id = rounded_rect(canvas, x, y, x + w, y + h, radius, fill=bg, outline="")
        self.text_id = canvas.create_text(x + w / 2, y + h / 2, text=text, fill=fg, font=font)
        for item in (self.rect_id, self.text_id):
            canvas.tag_bind(item, "<Enter>", self._on_enter)
            canvas.tag_bind(item, "<Leave>", self._on_leave)
            canvas.tag_bind(item, "<Button-1>", self._on_click)

    def _on_enter(self, event):
        if self.enabled:
            self.canvas.itemconfig(self.rect_id, fill=self.hover_bg)
            self.canvas.config(cursor="pointinghand" if IS_MAC else "hand2")

    def _on_leave(self, event):
        if self.enabled:
            self.canvas.itemconfig(self.rect_id, fill=self.bg)
        self.canvas.config(cursor="")

    def _on_click(self, event):
        if self.enabled and self.command:
            self.command()

    def set_text(self, text):
        self.canvas.itemconfig(self.text_id, text=text)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.canvas.itemconfig(self.rect_id, fill=self.bg if enabled else self.disabled_bg)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------
WIN_W = 460
ROW_H = 58
CARD_PAD = 20
CARD_Y1 = 100
CARD_Y2 = CARD_Y1 + CARD_PAD * 2 + ROW_H * len(TOOLS)
BTN_Y = CARD_Y2 + 26
BTN_H = 46
STATUS_Y = BTN_Y + BTN_H + 22
WIN_H = STATUS_Y + 46 + 22

LICENSE_URL = "https://github.com/bandusix/easy-codex-and-claude-cli-setup/blob/main/LICENSE"


class InstallerApp:
    def __init__(self, root):
        self.root = root
        self.lang = detect_language()
        self.installing = False

        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.resizable(False, False)
        self.root.configure(bg=COLOR_BG)

        self.canvas = tk.Canvas(root, width=WIN_W, height=WIN_H, bg=COLOR_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.lang_items = {}
        self.dynamic_texts = {}
        self.toggles = {}

        self._build_static_chrome()
        self._build_header()
        self._build_lang_switcher()
        self._build_card()
        self._build_button_and_status()
        self._build_footer()

        self.apply_language(self.lang)

    # -- fonts -------------------------------------------------------------
    def font(self, size, weight="normal"):
        return tkfont.Font(family=family_for(self.lang), size=size, weight=weight)

    # -- static chrome (things that never change with language) -----------
    def _build_static_chrome(self):
        c = self.canvas
        rounded_rect(c, 28, 28, 28 + 44, 28 + 44, RADIUS_ICON, fill=COLOR_ACCENT, outline="")
        c.create_text(28 + 22, 28 + 23, text=">_", fill="white",
                       font=tkfont.Font(family="Menlo" if IS_MAC else "Consolas", size=15, weight="bold"))

    def _build_header(self):
        c = self.canvas
        self.dynamic_texts["app_title"] = c.create_text(
            86, 40, anchor="w", fill=COLOR_TEXT, text="")
        self.dynamic_texts["app_subtitle"] = c.create_text(
            86, 62, anchor="w", fill=COLOR_SUBTEXT, text="")

    def _build_lang_switcher(self):
        c = self.canvas
        x = WIN_W - 28
        self.lang_items = {}
        for code in reversed(LANGS):
            label = LANG_LABELS[code]
            item = c.create_text(x, 32, anchor="e", text=label)
            c.tag_bind(item, "<Button-1>", lambda e, code=code: self.apply_language(code))
            c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="pointinghand" if IS_MAC else "hand2"))
            c.tag_bind(item, "<Leave>", lambda e: c.config(cursor=""))
            self.lang_items[code] = item
            bbox = c.bbox(item)
            x = bbox[0] - 12

    def _row_cy(self, index):
        return CARD_Y1 + CARD_PAD + ROW_H / 2 + index * ROW_H

    def _build_card(self):
        c = self.canvas
        x1, x2 = 28, WIN_W - 28
        rounded_rect(c, x1, CARD_Y1 + 3, x2, CARD_Y2 + 3, RADIUS_CARD, fill=COLOR_SHADOW, outline="")
        rounded_rect(c, x1, CARD_Y1, x2, CARD_Y2, RADIUS_CARD,
                     fill=COLOR_CARD, outline=COLOR_CARD_BORDER, width=1)

        text_x = 44 + 28 + 14
        toggle_x = x2 - 16 - 40

        for i, tool in enumerate(TOOLS):
            cy = self._row_cy(i)
            if i > 0:
                c.create_line(44, cy - ROW_H / 2, x2 - 16, cy - ROW_H / 2, fill=COLOR_DIVIDER)

            rounded_rect(c, 44, cy - 14, 44 + 28, cy + 14, RADIUS_ICON - 2, fill=tool["color"], outline="")
            c.create_text(44 + 14, cy, text=tool["icon"], fill="white", font=tkfont.Font(size=12, weight="bold"))

            self.dynamic_texts[f"{tool['id']}_title"] = c.create_text(
                text_x, cy - 9, anchor="w", fill=COLOR_TEXT, text="")
            self.dynamic_texts[f"{tool['id']}_desc"] = c.create_text(
                text_x, cy + 9, anchor="w", fill=COLOR_SUBTEXT, text="")

            self.toggles[tool["id"]] = ToggleSwitch(c, toggle_x, cy - 11, value=True)

    def _build_button_and_status(self):
        c = self.canvas
        self.btn = RoundedButton(
            c, 28, BTN_Y, WIN_W - 56, BTN_H, "", self.start_install,
            bg=COLOR_ACCENT, font=None,
        )
        self.status_id = c.create_text(WIN_W / 2, STATUS_Y, fill=COLOR_SUBTEXT, text="")

    def _build_footer(self):
        c = self.canvas
        self.footer_id = c.create_text(WIN_W / 2, WIN_H - 40, fill=COLOR_SUBTEXT, text="")

        credit_color = "#B0B0B3" if IS_MAC else "#A6A6A6"
        credit_id = c.create_text(
            WIN_W / 2, WIN_H - 18,
            text="Released under the MIT License · Copyright © 2026 bandusix",
            fill=credit_color, font=tkfont.Font(size=8),
        )
        c.tag_bind(credit_id, "<Button-1>", lambda e: webbrowser.open(LICENSE_URL))
        c.tag_bind(credit_id, "<Enter>", lambda e: c.config(cursor="pointinghand" if IS_MAC else "hand2"))
        c.tag_bind(credit_id, "<Leave>", lambda e: c.config(cursor=""))

    # -- language application ----------------------------------------------
    def apply_language(self, lang):
        self.lang = lang
        self.root.title(STRINGS[lang]["app_title"])

        f_title = self.font(17, "bold")
        f_sub = self.font(11)
        f_row_title = self.font(13, "bold")
        f_row_desc = self.font(10)
        f_btn = self.font(13, "bold")
        f_status = self.font(10)
        f_footer = self.font(9)
        f_lang = self.font(11, "bold")
        f_lang_inactive = self.font(11)

        c = self.canvas
        S = STRINGS[lang]
        c.itemconfig(self.dynamic_texts["app_title"], text=S["app_title"], font=f_title)
        c.itemconfig(self.dynamic_texts["app_subtitle"], text=S["app_subtitle"], font=f_sub)
        for tool in TOOLS:
            tid = tool["id"]
            c.itemconfig(self.dynamic_texts[f"{tid}_title"], text=S[f"{tid}_title"], font=f_row_title)
            c.itemconfig(self.dynamic_texts[f"{tid}_desc"], text=S[f"{tid}_desc"], font=f_row_desc)
        c.itemconfig(self.footer_id, text=S["footer_hint"], font=f_footer)

        if not self.installing:
            self.btn.set_text(S["install_button"])
        self.btn.canvas.itemconfig(self.btn.text_id, font=f_btn)
        c.itemconfig(self.status_id, text=STRINGS[lang]["status_idle"], font=f_status)

        for code, item in self.lang_items.items():
            active = code == lang
            c.itemconfig(item, fill=COLOR_ACCENT if active else COLOR_SUBTEXT,
                         font=f_lang if active else f_lang_inactive)

    # -- status helper -------------------------------------------------------
    def set_status(self, text, color):
        self.canvas.itemconfig(self.status_id, text=text, fill=color)

    # -- install lifecycle ---------------------------------------------------
    def start_install(self):
        if self.installing:
            return
        S = STRINGS[self.lang]
        selected = [tool for tool in TOOLS if self.toggles[tool["id"]].get()]
        if not selected:
            return

        self.installing = True
        self.btn.set_enabled(False)
        self.btn.set_text(S["installing_button"])
        self.set_status(S[f"status_installing_{selected[0]['id']}"], COLOR_SUBTEXT)

        thread = threading.Thread(target=self._install_worker, args=(selected,), daemon=True)
        thread.start()

    def _install_worker(self, selected):
        S = STRINGS[self.lang]
        try:
            for tool in selected:
                self.root.after(0, lambda t=tool: self.set_status(S[f"status_installing_{t['id']}"], COLOR_SUBTEXT))
                tool["install"]()
            self.root.after(0, self._on_install_success)
        except Exception as e:
            err = str(e)
            self.root.after(0, lambda: self._on_install_error(err))

    def _on_install_success(self):
        S = STRINGS[self.lang]
        self.installing = False
        self.btn.set_enabled(True)
        self.btn.set_text(S["install_button"])
        self.set_status(S["status_done"], COLOR_SUCCESS)
        bin_dir, _ = get_install_dirs()
        messagebox.showinfo(S["success_title"], S["success_body"].format(path=bin_dir))

    def _on_install_error(self, err):
        S = STRINGS[self.lang]
        self.installing = False
        self.btn.set_enabled(True)
        self.btn.set_text(S["install_button"])
        self.set_status(S["status_failed"], COLOR_ERROR)
        messagebox.showerror(S["error_title"], S["error_body"].format(error=err))


if __name__ == "__main__":
    root = tk.Tk()
    app = InstallerApp(root)
    root.mainloop()
