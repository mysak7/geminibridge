#!/usr/bin/env python3
"""
deploy.py — publish geminibridge to a remote host and run as systemd user services.

Compares a checksum of the deployable files against the last deployed checksum
stored on the remote. If anything changed (or --force is given), rsync the
project, install pip deps, and reload/restart the systemd services.

Usage:
    python publish/deploy.py [host] [--user USER] [--key KEY] [--force]
          [--api-key API_KEY] [--data-dir DIR]

Default host: gemini  (SSH alias configured in ~/.ssh/config as mi@gemini)
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_HOST = "gemini"
DEFAULT_USER = "mi"

# Files to deploy (relative to the project root)
DEPLOY_FILES = [
    "api.py",
    "chat_ui.py",
    "requirements.txt",
]

# Where the project lives on the remote
REMOTE_DIR = "~/geminibridge-app"

# Remote file that stores the last deployed checksum
REMOTE_CHECKSUM_FILE = f"{REMOTE_DIR}/.deployed_checksum"

# systemd user service names
SERVICE_API = "geminibridge"
SERVICE_UI  = "geminibridge-ui"

# ---------------------------------------------------------------------------
# Service file templates
# ---------------------------------------------------------------------------

API_SERVICE_TMPL = """\
[Unit]
Description=GeminiBridge API
After=network.target

[Service]
Type=simple
WorkingDirectory={remote_dir}
ExecStart={remote_dir}/venv/bin/uvicorn api:app --host 0.0.0.0 --port 8011
Restart=on-failure
RestartSec=5
Environment=PATH={gemini_bin_dir}:/usr/local/bin:/usr/bin:/bin
Environment=DB_PATH={data_dir}/chat_history.db
Environment=WORKSPACE={remote_dir}
Environment=API_KEY={api_key}

[Install]
WantedBy=default.target
"""

UI_SERVICE_TMPL = """\
[Unit]
Description=GeminiBridge Chat UI
After=network.target {api_service}.service

[Service]
Type=simple
WorkingDirectory={remote_dir}
ExecStart={remote_dir}/venv/bin/uvicorn chat_ui:app --host 0.0.0.0 --port 8012
Restart=on-failure
RestartSec=5
Environment=DB_PATH={data_dir}/chat_history.db
Environment=BRIDGE_URL=http://localhost:8011/v1/chat/completions
Environment=BRIDGE_KEY={api_key}

[Install]
WantedBy=default.target
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], check=True, capture=False, **kwargs) -> subprocess.CompletedProcess:
    kw = dict(check=check, **kwargs)
    if capture:
        kw["capture_output"] = True
        kw["text"] = True
    try:
        return subprocess.run(cmd, **kw)
    except subprocess.CalledProcessError as e:
        if capture and e.stderr:
            print(f"  stderr: {e.stderr.strip()}", file=sys.stderr)
        raise


def ssh_cmd(host: str, user: str, key: str | None, command: str, check=True) -> subprocess.CompletedProcess:
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd += ["-i", key]
    cmd += [f"{user}@{host}", command]
    return run(cmd, check=check, capture=True)


# ---------------------------------------------------------------------------
# Checksum
# ---------------------------------------------------------------------------

def local_checksum() -> str:
    """SHA-256 over the sorted content of all DEPLOY_FILES."""
    h = hashlib.sha256()
    for rel in sorted(DEPLOY_FILES):
        path = PROJECT_ROOT / rel
        if path.exists():
            h.update(rel.encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def remote_checksum(host: str, user: str, key: str | None) -> str:
    r = ssh_cmd(host, user, key, f"cat {REMOTE_CHECKSUM_FILE} 2>/dev/null || echo ''", check=False)
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# Deploy steps
# ---------------------------------------------------------------------------

def sync_files(host: str, user: str, key: str | None):
    print("[2/5] Syncing files via rsync...")
    ssh_cmd(host, user, key, f"mkdir -p {REMOTE_DIR}")
    for rel in DEPLOY_FILES:
        local_path = str(PROJECT_ROOT / rel)
        remote_path = f"{REMOTE_DIR}/{rel}"
        cmd = ["rsync", "-az"]
        if key:
            cmd += ["-e", f"ssh -i {key} -o StrictHostKeyChecking=accept-new"]
        else:
            cmd += ["-e", "ssh -o StrictHostKeyChecking=accept-new"]
        cmd += [local_path, f"{user}@{host}:{remote_path}"]
        run(cmd, check=True)
        print(f"  synced {rel}")


def install_deps(host: str, user: str, key: str | None):
    print("[3/5] Installing Python dependencies into venv...")

    # Install python3-venv only if venv doesn't exist yet (avoids apt lock on re-deploys)
    r = ssh_cmd(host, user, key, f"test -f {REMOTE_DIR}/venv/bin/activate && echo exists || echo missing", check=False)
    venv_exists = "exists" in r.stdout
    if not venv_exists:
        print("  venv not found — installing python3-venv via apt...")
        r = ssh_cmd(host, user, key,
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-full 2>&1 || true",
                    check=False)
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")

    # Create venv (no-op if exists) and install deps
    cmds = (
        f"python3 -m venv {REMOTE_DIR}/venv && "
        f"{REMOTE_DIR}/venv/bin/pip install -q --upgrade pip && "
        f"{REMOTE_DIR}/venv/bin/pip install -q -r {REMOTE_DIR}/requirements.txt"
    )
    try:
        r = ssh_cmd(host, user, key, cmds)
        for line in r.stdout.strip().splitlines():
            print(f"  {line}")
    except subprocess.CalledProcessError as e:
        print(f"  stdout: {e.stdout.strip()}", file=sys.stderr)
        print(f"  stderr: {e.stderr.strip()}", file=sys.stderr)
        raise


def install_services(host: str, user: str, key: str | None, data_dir: str, api_key: str):
    print("[4/5] Installing systemd user services...")

    remote_dir_expanded = f"/home/{user}/geminibridge-app"
    data_dir_expanded = data_dir.replace("~", f"/home/{user}")

    # Find the gemini CLI binary to set the correct PATH in the service
    r = ssh_cmd(host, user, key,
                "bash -lc 'which gemini 2>/dev/null || find ~/.nvm/versions -name gemini -type f 2>/dev/null | head -1'",
                check=False)
    gemini_path = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    gemini_bin_dir = gemini_path.rsplit("/", 1)[0] if "/" in gemini_path else "/usr/local/bin"
    print(f"  gemini binary : {gemini_path or '(not found)'}")

    api_unit = API_SERVICE_TMPL.format(
        remote_dir=remote_dir_expanded,
        data_dir=data_dir_expanded,
        api_key=api_key,
        gemini_bin_dir=gemini_bin_dir,
    )
    ui_unit = UI_SERVICE_TMPL.format(
        remote_dir=remote_dir_expanded,
        data_dir=data_dir_expanded,
        api_key=api_key,
        api_service=SERVICE_API,
    )

    service_dir = "~/.config/systemd/user"
    ssh_cmd(host, user, key, f"mkdir -p {service_dir} && mkdir -p {data_dir}")

    for name, content in [(SERVICE_API, api_unit), (SERVICE_UI, ui_unit)]:
        # Write service file by piping content via stdin
        cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
        if key:
            cmd += ["-i", key]
        cmd += [f"{user}@{host}", f"cat > {service_dir}/{name}.service"]
        run(cmd, check=True, input=content.encode())
        print(f"  wrote {service_dir}/{name}.service")

    # Reload daemon and enable+restart services
    ssh_cmd(host, user, key,
            "systemctl --user daemon-reload && "
            f"systemctl --user enable {SERVICE_API} {SERVICE_UI} && "
            f"systemctl --user restart {SERVICE_API} {SERVICE_UI}")
    print(f"  enabled and restarted {SERVICE_API}, {SERVICE_UI}")

    # Show status
    r = ssh_cmd(host, user, key,
                f"systemctl --user is-active {SERVICE_API} && "
                f"systemctl --user is-active {SERVICE_UI}",
                check=False)
    for line in r.stdout.strip().splitlines():
        print(f"  status: {line}")


def write_remote_checksum(host: str, user: str, key: str | None, checksum: str):
    print("[5/5] Recording deployed checksum on remote...")
    ssh_cmd(host, user, key, f"echo '{checksum}' > {REMOTE_CHECKSUM_FILE}")
    print(f"  checksum: {checksum[:16]}...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Deploy geminibridge to a remote host and run as systemd user services."
    )
    parser.add_argument("host", nargs="?", default=DEFAULT_HOST,
                        help=f"SSH hostname or alias (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"SSH user (default: {DEFAULT_USER})")
    parser.add_argument("--key", default=None,
                        help="Path to SSH private key (optional)")
    parser.add_argument("--force", action="store_true",
                        help="Deploy even if nothing has changed")
    parser.add_argument("--api-key", default="test",
                        help="API key for geminibridge (default: test)")
    parser.add_argument("--data-dir", default="~/geminibridge",
                        help="Remote dir for SQLite DB (default: ~/geminibridge)")
    args = parser.parse_args()

    host, user, key = args.host, args.user, args.key

    print(f"\n=== geminibridge deploy (no-docker) ===")
    print(f"  target  : {user}@{host}:{REMOTE_DIR}")
    print(f"  data    : {args.data_dir}")
    print(f"  services: {SERVICE_API}, {SERVICE_UI}")
    print()

    # 1. Compare checksums
    print("[1/5] Checking for changes...")
    local_cs = local_checksum()
    remote_cs = remote_checksum(host, user, key)
    print(f"  local  checksum : {local_cs[:16]}...")
    print(f"  remote checksum : {(remote_cs[:16] + '...') if remote_cs else '(none)'}")

    if local_cs == remote_cs and not args.force:
        print("\nNothing changed — remote is already up to date. Use --force to redeploy anyway.")
        return

    if local_cs == remote_cs and args.force:
        print("  No changes detected, but --force given — deploying anyway.")
    else:
        print("  Changes detected — deploying.")

    print()

    # 2. Sync
    sync_files(host, user, key)
    print()

    # 3. Pip install
    install_deps(host, user, key)
    print()

    # 4. Systemd services
    install_services(host, user, key, args.data_dir, args.api_key)
    print()

    # 5. Save checksum
    write_remote_checksum(host, user, key, local_cs)

    print("\nDeploy complete.")
    print(f"  API : http://{host}:8011/v1/chat/completions")
    print(f"  UI  : http://{host}:8012")


if __name__ == "__main__":
    main()
