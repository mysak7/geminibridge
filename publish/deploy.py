#!/usr/bin/env python3
"""
deploy.py — publish geminibridge to a remote host and refresh Docker if needed.

Compares a checksum of the deployable files against the last deployed checksum
stored on the remote. If anything changed (or --force is given), rsync the
project and rebuild/restart the Docker container.

Usage:
    python publish/deploy.py [host] [--user USER] [--key KEY] [--force]

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

# Files / dirs to deploy (relative to the project root)
DEPLOY_FILES = [
    "api.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
]

# Where the project lives on the remote
REMOTE_DIR = "~/geminibridge-app"

# Remote file that stores the last deployed checksum
REMOTE_CHECKSUM_FILE = f"{REMOTE_DIR}/.deployed_checksum"

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


def rsync(host: str, user: str, key: str | None, local_src: str, remote_dst: str):
    cmd = ["rsync", "-az", "--delete"]
    if key:
        cmd += ["-e", f"ssh -i {key} -o StrictHostKeyChecking=accept-new"]
    else:
        cmd += ["-e", "ssh -o StrictHostKeyChecking=accept-new"]
    cmd += [local_src, f"{user}@{host}:{remote_dst}"]
    run(cmd, check=True)


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
    print("[2/4] Syncing files via rsync...")
    # Ensure remote dir exists
    ssh_cmd(host, user, key, f"mkdir -p {REMOTE_DIR}")
    # Rsync each file individually (preserves structure without syncing the whole tree)
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


def docker_compose_bin(host: str, user: str, key: str | None) -> str:
    """Return working compose command, installing the v2 plugin from GitHub if necessary."""
    r = ssh_cmd(host, user, key, "docker compose version 2>/dev/null && echo __ok__ || true", check=False)
    if "__ok__" in r.stdout:
        return "docker compose"
    r2 = ssh_cmd(host, user, key, "docker-compose version 2>/dev/null && echo __ok__ || true", check=False)
    if "__ok__" in r2.stdout:
        return "docker-compose"
    # Neither found — download compose v2 plugin binary from GitHub (no sudo needed)
    print("  docker compose not found — installing v2 plugin to ~/.docker/cli-plugins/ ...")
    arch_r = ssh_cmd(host, user, key, "uname -m", check=False)
    arch = arch_r.stdout.strip()  # e.g. x86_64 or aarch64
    install_cmd = (
        "mkdir -p ~/.docker/cli-plugins && "
        f"curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-{arch}"
        " -o ~/.docker/cli-plugins/docker-compose && "
        "chmod +x ~/.docker/cli-plugins/docker-compose && "
        "docker compose version"
    )
    r3 = ssh_cmd(host, user, key, install_cmd)
    print(f"  {r3.stdout.strip()}")
    return "docker compose"


def rebuild_docker(host: str, user: str, key: str | None):
    print("[3/4] Rebuilding and restarting Docker container on remote...")
    compose = docker_compose_bin(host, user, key)
    print(f"  using: {compose}")
    # Ensure the data directory exists and is owned by the SSH user before
    # compose starts the container.  If Docker creates a missing bind-mount
    # dir it does so as root, making it unwritable by the node user (UID 1000).
    # Re-create if owned by root (empty dir is safe to remove).
    # Ensure the data directory exists and is writable by the container user.
    # The container runs as node (UID 1000); the host SSH user may have a
    # different UID.  If Docker created the dir as root, remove and recreate.
    # Then grant world-write so any UID inside the container can write to it.
    ssh_cmd(host, user, key,
            "mkdir -p ~/geminibridge && "
            "stat -c '%U' ~/geminibridge | grep -qv '^root$' || "
            "(rmdir ~/geminibridge 2>/dev/null; mkdir ~/geminibridge) && "
            "chmod a+rwx ~/geminibridge")
    compose_cmd = f"cd {REMOTE_DIR} && {compose} up -d --build 2>&1"
    r = ssh_cmd(host, user, key, compose_cmd)
    for line in r.stdout.strip().splitlines():
        print(f"  {line}")
    if r.stderr:
        for line in r.stderr.strip().splitlines():
            print(f"  {line}", file=sys.stderr)


def write_remote_checksum(host: str, user: str, key: str | None, checksum: str):
    print("[4/4] Recording deployed checksum on remote...")
    ssh_cmd(host, user, key, f"echo '{checksum}' > {REMOTE_CHECKSUM_FILE}")
    print(f"  checksum: {checksum[:16]}...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Deploy geminibridge to a remote host and refresh Docker if needed."
    )
    parser.add_argument("host", nargs="?", default=DEFAULT_HOST,
                        help=f"SSH hostname or alias (default: {DEFAULT_HOST})")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"SSH user (default: {DEFAULT_USER})")
    parser.add_argument("--key", default=None,
                        help="Path to SSH private key (optional, uses ~/.ssh/config if omitted)")
    parser.add_argument("--force", action="store_true",
                        help="Deploy even if nothing has changed")
    args = parser.parse_args()

    host, user, key = args.host, args.user, args.key

    print(f"\n=== geminibridge deploy ===")
    print(f"  target : {user}@{host}:{REMOTE_DIR}")
    print()

    # 1. Compare checksums
    print("[1/4] Checking for changes...")
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

    # 3. Rebuild Docker
    rebuild_docker(host, user, key)
    print()

    # 4. Save checksum
    write_remote_checksum(host, user, key, local_cs)

    print("\nDeploy complete.")


if __name__ == "__main__":
    main()
