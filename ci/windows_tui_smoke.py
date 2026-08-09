"""Interactive-TUI smoke test for Kimi Code on Windows (ConPTY).

The main smoke test (windows_smoke.py) exercises `kimi -p` print mode, but a
reported field failure is bare `kimi` exiting instantly and silently in a real
terminal — a path only the interactive TUI hits. This test spawns kimi in a
ConPTY pseudo-terminal (the closest thing to a real console), watches the
screen for up to 30s, and fails unless the TUI actually renders.

Run by .github/workflows/smoke-windows.yml after windows_smoke.py.
"""

import os
import sys
import time

# Runner consoles are cp1252 — TUI output contains CJK/box-drawing chars.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WATCH_SECONDS = 30
# Any of these proves the TUI booted: the first-launch trust dialog, or the
# main chat UI banner.
TUI_MARKERS = ("Trust this folder", "Kimi Code", "kimi", "What can I")


def main():
    from winpty import PtyProcess  # pywinpty, installed by the workflow

    bin_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "ai_tools_bin")
    shim = os.path.join(bin_dir, "kimi.cmd")
    if not os.path.exists(shim):
        print(f"FATAL: shim missing: {shim}")
        return 1

    env = os.environ.copy()
    node_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs",
                            "ai_tools_env", "node")
    env["PATH"] = bin_dir + os.pathsep + node_dir + os.pathsep + env.get("PATH", "")

    print(f"spawning: {shim} (in ConPTY)", flush=True)
    proc = PtyProcess.spawn(["cmd.exe", "/c", shim], dimensions=(30, 120), env=env)

    # PtyProcess.read() BLOCKS when the TUI goes idle — pump it on a daemon
    # thread so the watchdog below can always fire.
    chunks = []
    import threading

    def _pump():
        while True:
            try:
                data = proc.read()
            except Exception:
                return
            if not data:
                return
            chunks.append(data if isinstance(data, bytes) else data.encode())

    threading.Thread(target=_pump, daemon=True).start()

    deadline = time.time() + WATCH_SECONDS
    while time.time() < deadline:
        text_now = b"".join(chunks).decode("utf-8", errors="replace")
        if any(m in text_now for m in TUI_MARKERS):
            break  # TUI rendered — no need to watch longer
        if not proc.isalive() and chunks:
            break  # exited with output (or silence) already captured
        time.sleep(0.5)

    alive = proc.isalive()
    buf = b"".join(chunks)
    text = buf.decode("utf-8", errors="replace")
    print(f"\n--- captured {len(buf)} bytes, process alive={alive} ---", flush=True)
    # Strip ANSI escapes for the log dump.
    import re
    clean = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
    clean = re.sub(r"[\x00-\x08\x0b-\x1f]", "", clean)
    lines = [l for l in clean.splitlines() if l.strip()]
    print("\n".join(lines[:40]), flush=True)
    print("--- end capture ---", flush=True)

    try:
        if alive:
            proc.write("\x1b")  # Esc: leave any dialog / exit the TUI
            time.sleep(2)
        proc.close()
    except Exception:
        pass

    if any(m in text for m in TUI_MARKERS):
        print("\nTUI SMOKE RESULT: PASS (kimi interactive UI rendered)", flush=True)
        return 0

    print("\nTUI SMOKE RESULT: FAIL (kimi produced no TUI output in a real "
          "terminal — this reproduces the silent-exit field report)", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
