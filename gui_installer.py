import os
import sys
import json
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


def _broadcast_env_change():
    """Tell Explorer & friends to reload the environment, so NEW terminals see
    PATH changes immediately. Without this a logoff/reboot would be required."""
    import ctypes
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    try:
        result = ctypes.c_ulong(0)
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result))
    except Exception:
        pass


def add_to_path_win(target_dir):
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_ALL_ACCESS)
        try:
            current_path, _ = winreg.QueryValueEx(key, "PATH")
        except FileNotFoundError:
            current_path = ""
        entries = [e for e in current_path.split(";") if e.strip()]
        if target_dir.lower() not in [e.lower() for e in entries]:
            entries.append(target_dir)
            # Write the registry directly — never `setx`: it truncates PATH at
            # 1024 chars and flattens REG_EXPAND_SZ, silently corrupting it.
            winreg.SetValueEx(key, "PATH", 0, winreg.REG_EXPAND_SZ, ";".join(entries))
            _broadcast_env_change()
        winreg.CloseKey(key)
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
        _expose_node_runtime(node_dir)
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

    _expose_node_runtime(node_dir)
    return node_dir


def _expose_node_runtime(node_dir):
    """Make node/npm/npx themselves usable from any terminal, not just the
    tool shims — students need them for their own projects (npm install etc.)."""
    bin_dir, _ = get_install_dirs()
    if IS_WIN:
        # node.exe / npm.cmd / npx.cmd all live in node_dir on Windows.
        add_to_path_win(node_dir)
    else:
        for name in ("node", "npm", "npx"):
            src = os.path.join(node_dir, "bin", name)
            dst = os.path.join(bin_dir, name)
            if os.path.exists(src) and not os.path.exists(dst):
                os.symlink(src, dst)


def _npm_bin(node_dir):
    if IS_MAC:
        return os.path.join(node_dir, "bin", "npm")
    return os.path.join(node_dir, "npm.cmd")


def _npm_env(node_dir):
    """Env for npm subprocesses: put the bundled node on PATH so lifecycle
    scripts (postinstall etc.) that shell out to `node` can find it — the
    portable runtime is otherwise invisible to child processes."""
    env = os.environ.copy()
    extra = node_dir if IS_WIN else os.path.join(node_dir, "bin")
    env["PATH"] = extra + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------------------
# LLM provider configuration (writes each CLI's native config so the tools
# work out of the box against a relay/plan endpoint — no student-side setup)
# ---------------------------------------------------------------------------
def _load_builtin_llm_config():
    """Builtin provider config injected at CI build time (payload/builtin_config.json).
    Never committed to the repo — absent in local/dev runs."""
    path = get_resource_path("payload/builtin_config.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _set_user_env(name, value):
    """Persist a user-level environment variable (new terminals only)."""
    if IS_WIN:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_ALL_ACCESS)
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        _broadcast_env_change()
    else:
        rc = os.path.expanduser("~/.zshrc")
        line = f'export {name}="{value}"'
        existing = ""
        if os.path.exists(rc):
            with open(rc, "r", encoding="utf-8") as f:
                existing = f.read()
        if line not in existing:
            with open(rc, "a", encoding="utf-8") as f:
                f.write("\n" + line + "\n")


def _read_json_file(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def configure_claude_code(cfg):
    home = os.path.expanduser("~")
    settings_path = os.path.join(home, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    data = _read_json_file(settings_path)
    env = data.get("env", {})
    env.update(cfg["anthropic_env"])
    data["env"] = env
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Skip the first-run onboarding wizard.
    claude_json = os.path.join(home, ".claude.json")
    cdata = _read_json_file(claude_json)
    cdata["hasCompletedOnboarding"] = True
    with open(claude_json, "w", encoding="utf-8") as f:
        json.dump(cdata, f, indent=2, ensure_ascii=False)


def configure_codex(cfg):
    codex_dir = os.path.join(os.path.expanduser("~"), ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    config_path = os.path.join(codex_dir, "config.toml")
    if os.path.exists(config_path):
        shutil.copy(config_path, config_path + ".bak")
    # Codex removed wire_api="chat" (openai/codex#7782) — must use the
    # Responses API, and the model must be enabled for it (step-3.7-flash is,
    # step-3.5-flash-2603 is not).
    toml = (
        f'model = "{cfg.get("codex_model", cfg["model"])}"\n'
        f'model_provider = "relay"\n'
        # The default read-only sandbox needs a per-machine admin sandbox setup
        # on Windows that fails on fresh machines with a blocking interactive
        # prompt; full access skips it (Claude Code likewise skips its
        # permission prompts via skipDangerousModePermissionPrompt).
        f'sandbox_mode = "danger-full-access"\n'
        f"\n"
        f'[model_providers.relay]\n'
        f'name = "{cfg["name"]}"\n'
        f'base_url = "{cfg["openai_base_url"]}"\n'
        f'wire_api = "responses"\n'
        f'env_key = "RELAY_API_KEY"\n'
    )
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(toml)
    _set_user_env("RELAY_API_KEY", cfg["openai_api_key"])


def configure_kimi(cfg):
    """Kimi Code reads ~/.kimi-code/config.toml only (never shell env vars).
    Step's endpoint speaks the Anthropic Messages protocol, so we register it
    as an `anthropic` provider with a default model alias."""
    kimi_dir = os.path.join(os.path.expanduser("~"), ".kimi-code")
    os.makedirs(kimi_dir, exist_ok=True)
    config_path = os.path.join(kimi_dir, "config.toml")
    if os.path.exists(config_path):
        shutil.copy(config_path, config_path + ".bak")
    env = cfg["anthropic_env"]
    toml = (
        f'default_model = "relay/step"\n'
        f"\n"
        f'[providers.relay]\n'
        f'type = "anthropic"\n'
        f'base_url = "{env["ANTHROPIC_BASE_URL"]}"\n'
        f'api_key = "{env["ANTHROPIC_AUTH_TOKEN"]}"\n'
        f"\n"
        f'[models."relay/step"]\n'
        f'provider = "relay"\n'
        f'model = "{cfg["model"]}"\n'
        f'max_context_size = 262144\n'
        f'capabilities = [ "thinking", "tool_use" ]\n'
    )
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(toml)


def apply_llm_config(selected_ids, cfg):
    """Write provider config for whichever supported tools were installed."""
    if "claude" in selected_ids:
        configure_claude_code(cfg)
    if "codex" in selected_ids:
        configure_codex(cfg)
    if "kimi" in selected_ids:
        configure_kimi(cfg)


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
        if not os.path.exists(node_shim):
            raise Exception(f"npm did not create the expected shim: {node_shim}")
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


def install_kimi():
    bin_dir, app_dir = get_install_dirs()
    node_dir = _ensure_node(app_dir)
    npm_bin = _npm_bin(node_dir)

    tgz = _find_payload_tgz("moonshot-ai-kimi-code")
    if not tgz:
        raise Exception("Kimi Code CLI npm package not found in payload.")

    # Wipe leftovers from a previous failed install: npm's rollback can leave a
    # partial package tree behind, and reinstalling over it yields a broken CLI
    # that exits silently on launch.
    shutil.rmtree(os.path.join(node_dir, "node_modules", "@moonshot-ai"),
                  ignore_errors=True)
    for shim in ("kimi", "kimi.cmd"):
        stale_shim = os.path.join(node_dir, shim)
        if os.path.exists(stale_shim):
            os.remove(stale_shim)

    _run_npm([npm_bin, "install", "-g", "--prefix", node_dir, tgz], _npm_env(node_dir))
    _expose_shim(bin_dir, node_dir, "kimi")


# ---------------------------------------------------------------------------
# Tool registry — drives both the GUI rows and the install worker
# ---------------------------------------------------------------------------
TOOLS = [
    {"id": "codex", "icon": "C", "color": "#3B82C4", "install": install_codex},
    {"id": "claude", "icon": "A", "color": "#CC785C", "install": install_claude_code},
    {"id": "kimi", "icon": "K", "color": "#0EA5A6", "install": install_kimi},
]


# ---------------------------------------------------------------------------
# Localization
# ---------------------------------------------------------------------------
LANGS = ["en", "zh-Hans", "zh-Hant"]
LANG_LABELS = {"en": "EN", "zh-Hans": "简", "zh-Hant": "繁"}

STRINGS = {
    "en": {
        "app_title": "AI Tools Installer",
        "app_subtitle": "Set up 3 AI coding CLIs — fully offline",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI's coding agent for your terminal",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic's coding agent for your terminal",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI's coding agent for your terminal",
        "install_button": "Install Now",
        "installing_button": "Installing…",
        "status_idle": "",
        "status_installing_codex": "Installing Codex CLI…",
        "status_installing_claude": "Installing Claude Code (Node.js)…",
        "status_installing_kimi": "Installing Kimi Code CLI (Node.js)…",
        "status_done": "Installation complete",
        "status_failed": "Installation failed",
        "success_title": "Success",
        "success_body": "Installation successful!\n\nInstalled to:\n{path}\n\nRestart your terminal to use the tools you installed.",
        "error_title": "Error",
        "error_body": "Something went wrong:\n{error}",
        "footer_hint": "Uncheck a tool to skip installing it.",
        "cfg_title": "Model key setup (Claude Code / Codex / Kimi Code)",
        "cfg_builtin": "Auto-configure (built-in Step Plan)",
        "cfg_manual_link": "Use my own key…",
        "cfg_mode_manual": "custom key saved",
        "status_configuring": "Writing model configuration…",
        "dlg_title": "Custom Provider Key",
        "dlg_base_url": "Base URL",
        "dlg_api_key": "API Key",
        "dlg_model": "Model",
        "dlg_codex_model": "Codex model (Responses API)",
        "dlg_save": "Save",
        "dlg_cancel": "Cancel",
    },
    "zh-Hans": {
        "app_title": "AI 工具安装向导",
        "app_subtitle": "离线安装 3 款 AI 编程 CLI",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI 出品的终端编程助手",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic 出品的终端编程助手",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI 出品的终端编程助手",
        "install_button": "立即安装",
        "installing_button": "安装中…",
        "status_idle": "",
        "status_installing_codex": "正在安装 Codex CLI…",
        "status_installing_claude": "正在安装 Claude Code (Node.js)…",
        "status_installing_kimi": "正在安装 Kimi Code CLI (Node.js)…",
        "status_done": "安装完成",
        "status_failed": "安装失败",
        "success_title": "安装成功",
        "success_body": "安装成功！\n\n已安装至：\n{path}\n\n请重启终端后使用已安装的工具。",
        "error_title": "出错了",
        "error_body": "安装过程中出现错误：\n{error}",
        "footer_hint": "取消勾选可跳过对应工具的安装。",
        "cfg_title": "模型 Key 配置（Claude Code / Codex / Kimi Code）",
        "cfg_builtin": "自动配置（内置 Step Plan）",
        "cfg_manual_link": "使用自己的 Key…",
        "cfg_mode_manual": "已保存自定义 Key",
        "status_configuring": "正在写入模型配置…",
        "dlg_title": "自定义模型 Key",
        "dlg_base_url": "接口地址 (Base URL)",
        "dlg_api_key": "API Key",
        "dlg_model": "模型名",
        "dlg_codex_model": "Codex 模型（需支持 Responses）",
        "dlg_save": "保存",
        "dlg_cancel": "取消",
    },
    "zh-Hant": {
        "app_title": "AI 工具安裝精靈",
        "app_subtitle": "離線安裝 3 款 AI 程式設計 CLI",
        "codex_title": "Codex CLI",
        "codex_desc": "OpenAI 推出的終端機程式設計助手",
        "claude_title": "Claude Code CLI",
        "claude_desc": "Anthropic 推出的終端機程式設計助手",
        "kimi_title": "Kimi Code CLI",
        "kimi_desc": "Moonshot AI 推出的終端機程式設計助手",
        "install_button": "立即安裝",
        "installing_button": "安裝中…",
        "status_idle": "",
        "status_installing_codex": "正在安裝 Codex CLI…",
        "status_installing_claude": "正在安裝 Claude Code (Node.js)…",
        "status_installing_kimi": "正在安裝 Kimi Code CLI (Node.js)…",
        "status_done": "安裝完成",
        "status_failed": "安裝失敗",
        "success_title": "安裝成功",
        "success_body": "安裝成功！\n\n已安裝至：\n{path}\n\n請重新啟動終端機後使用已安裝的工具。",
        "error_title": "發生錯誤",
        "error_body": "安裝過程中發生錯誤：\n{error}",
        "footer_hint": "取消勾選可略過該工具的安裝。",
        "cfg_title": "模型 Key 設定（Claude Code / Codex / Kimi Code）",
        "cfg_builtin": "自動設定（內建 Step Plan）",
        "cfg_manual_link": "使用自己的 Key…",
        "cfg_mode_manual": "已儲存自訂 Key",
        "status_configuring": "正在寫入模型設定…",
        "dlg_title": "自訂模型 Key",
        "dlg_base_url": "介面位址 (Base URL)",
        "dlg_api_key": "API Key",
        "dlg_model": "模型名稱",
        "dlg_codex_model": "Codex 模型（需支援 Responses）",
        "dlg_save": "儲存",
        "dlg_cancel": "取消",
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
CFG_Y1 = CARD_Y2 + 16
CFG_H = 100
CFG_Y2 = CFG_Y1 + CFG_H
BTN_Y = CFG_Y2 + 22
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
        self._build_config_card()
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

    def _build_config_card(self):
        c = self.canvas
        x1, x2 = 28, WIN_W - 28
        rounded_rect(c, x1, CFG_Y1 + 3, x2, CFG_Y2 + 3, RADIUS_CARD, fill=COLOR_SHADOW, outline="")
        rounded_rect(c, x1, CFG_Y1, x2, CFG_Y2, RADIUS_CARD,
                     fill=COLOR_CARD, outline=COLOR_CARD_BORDER, width=1)

        self.manual_cfg = None
        self.dynamic_texts["cfg_title"] = c.create_text(44, CFG_Y1 + 24, anchor="w", fill=COLOR_TEXT, text="")
        self.dynamic_texts["cfg_builtin"] = c.create_text(44, CFG_Y1 + 50, anchor="w", fill=COLOR_SUBTEXT, text="")

        self.cfg_toggle = ToggleSwitch(c, x2 - 16 - 40, CFG_Y1 + 39, value=True)

        link = c.create_text(44, CFG_Y1 + 78, anchor="w", fill=COLOR_ACCENT, text="")
        c.tag_bind(link, "<Button-1>", lambda e: self._open_manual_cfg_dialog())
        c.tag_bind(link, "<Enter>", lambda e: c.config(cursor="pointinghand" if IS_MAC else "hand2"))
        c.tag_bind(link, "<Leave>", lambda e: c.config(cursor=""))
        self.dynamic_texts["cfg_manual_link"] = link
        self.cfg_mode_text = c.create_text(x2 - 16, CFG_Y1 + 78, anchor="e", fill=COLOR_SUCCESS, text="")

    def _open_manual_cfg_dialog(self):
        S = STRINGS[self.lang]
        dlg = tk.Toplevel(self.root)
        dlg.title(S["dlg_title"])
        dlg.configure(bg=COLOR_CARD)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        fields = {}
        defaults = {
            "base_url": "https://api.stepfun.com/step_plan",
            "api_key": "",
            "model": "step-3.5-flash-2603",
            "codex_model": "step-3.7-flash",
        }
        if self.manual_cfg:
            defaults["base_url"] = self.manual_cfg["openai_base_url"].removesuffix("/v1")
            defaults["api_key"] = self.manual_cfg["openai_api_key"]
            defaults["model"] = self.manual_cfg["model"]
            defaults["codex_model"] = self.manual_cfg.get("codex_model", "step-3.7-flash")
        for i, key in enumerate(("base_url", "api_key", "model", "codex_model")):
            tk.Label(dlg, text=S[f"dlg_{key}"], bg=COLOR_CARD, fg=COLOR_TEXT,
                     font=self.font(10)).grid(row=i, column=0, sticky="w", padx=16, pady=(14 if i == 0 else 6))
            entry = tk.Entry(dlg, width=38, font=self.font(10), show="" if key != "api_key" else "•")
            entry.insert(0, defaults[key])
            entry.grid(row=i, column=1, padx=(4, 16), pady=(14 if i == 0 else 6))
            fields[key] = entry

        def save():
            base_url = fields["base_url"].get().strip().rstrip("/")
            api_key = fields["api_key"].get().strip()
            model = fields["model"].get().strip() or "step-3.5-flash-2603"
            codex_model = fields["codex_model"].get().strip() or "step-3.7-flash"
            if not base_url or not api_key:
                return
            self.manual_cfg = {
                "name": "Custom",
                "anthropic_env": {
                    "ANTHROPIC_AUTH_TOKEN": api_key,
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                },
                "openai_base_url": base_url + "/v1",
                "openai_api_key": api_key,
                "model": model,
                "codex_model": codex_model,
            }
            self.canvas.itemconfig(self.cfg_mode_text, text=STRINGS[self.lang]["cfg_mode_manual"])
            dlg.destroy()

        btns = tk.Frame(dlg, bg=COLOR_CARD)
        btns.grid(row=4, column=0, columnspan=2, pady=14)
        tk.Button(btns, text=S["dlg_save"], command=save, width=8).pack(side="left", padx=8)
        tk.Button(btns, text=S["dlg_cancel"], command=dlg.destroy, width=8).pack(side="left", padx=8)
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

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
        c.itemconfig(self.dynamic_texts["cfg_title"], text=S["cfg_title"], font=f_row_title)
        c.itemconfig(self.dynamic_texts["cfg_builtin"], text=S["cfg_builtin"], font=f_row_desc)
        c.itemconfig(self.dynamic_texts["cfg_manual_link"], text=S["cfg_manual_link"], font=f_row_desc)

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
            cfg = self.manual_cfg or (_load_builtin_llm_config() if self.cfg_toggle.get() else None)
            if cfg:
                self.root.after(0, lambda: self.set_status(S["status_configuring"], COLOR_SUBTEXT))
                apply_llm_config([t["id"] for t in selected], cfg)
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
