"""Windows end-to-end smoke test for the AI Tools Installer.

Runs the REAL install + configure functions from gui_installer.py on a clean
machine (a GitHub Actions windows-latest runner), then executes each CLI
non-interactively against the builtin relay — exactly what a student would do.

Invoked by .github/workflows/smoke-windows.yml. Exits non-zero on any failure
so a broken tool turns the CI run red.
"""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import gui_installer as g

TIMEOUT = 300


def run(cmd, env, check=True):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, timeout=TIMEOUT,
                           text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {TIMEOUT}s", flush=True)
        return not check
    except Exception as e:
        print(f"FAILED TO LAUNCH: {e}", flush=True)
        return not check
    print(f"exit={p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}",
          flush=True)
    if not check:
        return True
    return p.returncode == 0 and bool((p.stdout or "").strip())


def main():
    # 1) Real install, same code path as the GUI's Install button.
    bin_dir, app_dir = g.get_install_dirs()
    g.install_codex()
    g.install_claude_code()
    g.install_kimi()

    # 2) Real provider configuration from the injected builtin config.
    cfg = g._load_builtin_llm_config()
    if not cfg:
        print("FATAL: payload/builtin_config.json missing "
              "(BUILTIN_LLM_CONFIG secret not injected?)")
        return 1
    g.apply_llm_config(["codex", "claude", "kimi"], cfg)

    # 3) Mimic a fresh terminal: bin_dir + node_dir on PATH, plus the registry
    # env vars that configure_* persisted (this process predates them).
    node_dir = os.path.join(app_dir, "node")
    env = os.environ.copy()
    env["PATH"] = (bin_dir + os.pathsep + node_dir + os.pathsep
                   + env.get("PATH", ""))
    env["RELAY_API_KEY"] = cfg["openai_api_key"]

    ok = True
    # Version probes are informational; the model round-trips are the gate.
    ok &= run([os.path.join(bin_dir, "codex.exe"), "--version"], env, check=False)
    ok &= run([os.path.join(bin_dir, "codex.exe"), "exec", "say hi"], env)
    ok &= run([os.path.join(bin_dir, "claude.exe"), "--version"], env, check=False)
    ok &= run([os.path.join(bin_dir, "claude.exe"), "-p", "say hi"], env)
    ok &= run(["cmd.exe", "/c", os.path.join(node_dir, "kimi.cmd"), "--version"],
              env, check=False)
    ok &= run(["cmd.exe", "/c", os.path.join(node_dir, "kimi.cmd"), "-p", "say hi"],
              env)

    print(f"\nSMOKE RESULT: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
