#!/usr/bin/env bash
# Run as the checkout owner, e.g. infoman. Only deployment uses sudo.
set -Eeuo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$SOURCE"
[[ $EUID -ne 0 ]] || { echo 'Run as the checkout owner, without sudo.' >&2; exit 1; }
[[ -d .git ]] || { echo 'Clone the repository with git first.' >&2; exit 1; }
exec 9>.git/ubuntu-monitor-update.lock
flock -n 9 || { echo 'Another update is running.' >&2; exit 1; }
[[ $(git branch --show-current) == main ]] || { echo 'Switch to branch main first.' >&2; exit 1; }
[[ -z $(git status --porcelain --untracked-files=normal) ]] || {
    echo 'Commit or stash local changes before updating.' >&2; exit 1;
}
git fetch --prune origin
git merge-base --is-ancestor HEAD origin/main || {
    echo 'Local main diverges from origin/main; resolve it manually.' >&2; exit 1;
}
git merge --ff-only origin/main
# Install even if HEAD is unchanged, so a failed deployment can be retried.
sudo bash "$SOURCE/scripts/install.sh"
