#!/usr/bin/env python3
"""
install_gemini_cli.py

Installs Gemini CLI on a remote host via SSH and uploads ~/.gemini/settings.json
from the local machine. Works with Amazon Linux 2023 (dnf) and Ubuntu/Debian (apt).

Usage:
    python install_gemini_cli.py [host] [--user USER] [--key KEY] [--settings PATH]

Default host: gemini (configured in ~/.ssh/config as mi@gemini)
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path


# --- config defaults -------------------------------------------------------

DEFAULT_HOST = "gemini"
DEFAULT_USER = "mi"
DEFAULT_SETTINGS = str(Path.home() / ".gemini" / "settings.json")
NVM_VERSION = "v0.40.1"
NODE_VERSION = "22"
GEMINI_PACKAGE = "@google/gemini-cli"

# ---------------------------------------------------------------------------


def run(cmd: list[str], check=True, capture=False) -> subprocess.CompletedProcess:
    kwargs = dict(check=check)
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    else:
        kwargs["stdout"] = None
        kwargs["stderr"] = None
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.CalledProcessError as e:
        if capture and e.stderr:
            print(f"  STDERR: {e.stderr.strip()}", file=sys.stderr)
        raise


def ssh(host: str, user: str, key: str | None, command: str, check=True) -> subprocess.CompletedProcess:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd += ["-i", key]
    cmd += [f"{user}@{host}", command]
    return run(cmd, check=check, capture=True)


def scp_upload(host: str, user: str, key: str | None, local: str, remote: str):
    cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd += ["-i", key]
    cmd += [local, f"{user}@{host}:{remote}"]
    run(cmd, check=True)


def detect_os(host: str, user: str, key: str | None) -> str:
    """Returns 'amazon' | 'debian' | 'unknown'"""
    r = ssh(host, user, key, "cat /etc/os-release 2>/dev/null", check=False)
    content = r.stdout.lower()
    if "amazon linux" in content:
        return "amazon"
    if "ubuntu" in content or "debian" in content or "raspbian" in content or "raspberry" in content:
        return "debian"
    return "unknown"


def node_version_ok(host: str, user: str, key: str | None) -> bool:
    """Returns True if Node.js >= 20 is already available (via nvm or system)."""
    check_cmd = (
        'export NVM_DIR="$HOME/.nvm"; '
        '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
        'node -e "process.exit(parseInt(process.version.slice(1)) >= 20 ? 0 : 1)" 2>/dev/null'
    )
    r = ssh(host, user, key, check_cmd, check=False)
    return r.returncode == 0


def install_nvm_node(host: str, user: str, key: str | None):
    print(f"  Installing nvm {NVM_VERSION} and Node.js {NODE_VERSION}...")
    install_cmd = (
        f'curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/{NVM_VERSION}/install.sh | bash && '
        f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && '
        f'nvm install {NODE_VERSION} && nvm alias default {NODE_VERSION} && '
        f'node --version && npm --version'
    )
    r = ssh(host, user, key, install_cmd)
    print(r.stdout.strip())


def install_system_packages(host: str, user: str, key: str | None, os_type: str):
    """Install curl (needed for nvm) via system package manager."""
    if os_type == "amazon":
        cmd = "command -v curl &>/dev/null || sudo dnf install -y curl"
    elif os_type == "debian":
        cmd = "command -v curl &>/dev/null || (sudo apt-get update -qq && sudo apt-get install -y curl)"
    else:
        return
    print(f"  Ensuring curl is present ({os_type})...")
    ssh(host, user, key, cmd)


def install_gemini_cli(host: str, user: str, key: str | None):
    print(f"  Installing {GEMINI_PACKAGE}...")
    install_cmd = (
        f'export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && '
        f'npm install -g {GEMINI_PACKAGE} 2>&1 | tail -3 && '
        f'gemini --version'
    )
    r = ssh(host, user, key, install_cmd)
    print(r.stdout.strip())


def upload_settings(host: str, user: str, key: str | None, settings_path: str):
    print(f"  Creating ~/.gemini and uploading settings.json...")
    ssh(host, user, key, "mkdir -p ~/.gemini")
    scp_upload(host, user, key, settings_path, "~/.gemini/settings.json")
    r = ssh(host, user, key, "cat ~/.gemini/settings.json")
    print("  Uploaded settings.json:")
    for line in r.stdout.strip().splitlines():
        print(f"    {line}")


def ensure_nvm_in_bashrc(host: str, user: str, key: str | None):
    """Make sure nvm is sourced in .bashrc (nvm installer does this, but double-check)."""
    check = ssh(host, user, key, 'grep -q "NVM_DIR" ~/.bashrc && echo ok || echo missing', check=False)
    if "missing" in check.stdout:
        append_cmd = (
            'echo \'export NVM_DIR="$HOME/.nvm"\' >> ~/.bashrc && '
            'echo \'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\' >> ~/.bashrc'
        )
        ssh(host, user, key, append_cmd)
        print("  Added nvm source lines to ~/.bashrc")


def main():
    parser = argparse.ArgumentParser(description="Install Gemini CLI on a remote host via SSH.")
    parser.add_argument("host", nargs="?", default=DEFAULT_HOST,
                        help=f"Remote hostname or SSH alias (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"SSH user (default: {DEFAULT_USER})")
    parser.add_argument("--key", default=None, help="Path to SSH private key (optional, uses ~/.ssh/config if omitted)")
    parser.add_argument("--settings", default=DEFAULT_SETTINGS,
                        help=f"Local settings.json path (default: {DEFAULT_SETTINGS})")
    args = parser.parse_args()

    host, user, key, settings_path = args.host, args.user, args.key, args.settings

    print(f"\n=== Gemini CLI installer ===")
    print(f"  Host     : {user}@{host}")
    print(f"  Settings : {settings_path}")
    if key:
        print(f"  Key      : {key}")
    print()

    # 1. Detect OS
    print("[1/5] Detecting OS...")
    os_type = detect_os(host, user, key)
    print(f"  OS type: {os_type}")

    if os_type == "unknown":
        print("  WARNING: unknown OS, will attempt anyway.")

    # 2. Ensure curl
    print("[2/5] Checking system dependencies...")
    install_system_packages(host, user, key, os_type)

    # 3. Install Node.js via nvm if needed
    print("[3/5] Checking Node.js...")
    if node_version_ok(host, user, key):
        print("  Node.js >= 20 already available, skipping nvm install.")
    else:
        install_nvm_node(host, user, key)
        ensure_nvm_in_bashrc(host, user, key)

    # 4. Install Gemini CLI
    print("[4/5] Installing Gemini CLI...")
    install_gemini_cli(host, user, key)

    # 5. Upload settings
    print("[5/5] Uploading settings.json...")
    if not os.path.isfile(settings_path):
        print(f"  ERROR: settings file not found: {settings_path}")
        sys.exit(1)
    upload_settings(host, user, key, settings_path)

    print("\nDone! Run `gemini` on the remote host (new SSH session needed if nvm was freshly installed).")


if __name__ == "__main__":
    main()
