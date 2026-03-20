#!/usr/bin/env bash
# Run this ON the gemini host to authorize mi@ras (geminibridge installer machine).
# After this, the install script can SSH in without prompts.
set -euo pipefail
mkdir -p ~/.ssh
chmod 700 ~/.ssh
KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFGaVDozSjIrySDI5UjF5gejQh17hP+DmKWprnbWPcYs mi@ras"
grep -qF "$KEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$KEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo "Done — mi@ras is now authorized."
