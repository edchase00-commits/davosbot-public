#!/bin/sh
# Install repo-managed Git hooks for the current DavosBot checkout.

set -eu

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push .githooks/post-merge .githooks/commit-msg 2>/dev/null || true

echo "DavosBot git hooks installed: core.hooksPath=.githooks"
