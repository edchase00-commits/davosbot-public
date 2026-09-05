#!/usr/bin/env bash
set -euo pipefail

export DAVOSBOT_SUPPRESS_CONFIG_WARNINGS=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  PROD_DIR="${DAVOSBOT_PROD_DIR:-}"
  if [[ -z "$PROD_DIR" && -n "${HOME:-}" ]]; then
    DEFAULT_PROD_DIR="$HOME/projects/davosbot"
    if [[ -d "$DEFAULT_PROD_DIR" ]]; then
      PROD_DIR="$DEFAULT_PROD_DIR"
    fi
  fi
  if [[ -z "$PROD_DIR" && "$ROOT_DIR" == *"/.auto_deploy/worktrees/"* ]]; then
    PROD_DIR="${ROOT_DIR%%/.auto_deploy/worktrees/*}"
  fi
  if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/venv/bin/python"
  elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  elif [[ -n "$PROD_DIR" && -x "$PROD_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$PROD_DIR/venv/bin/python"
  elif [[ -n "$PROD_DIR" && -x "$PROD_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PROD_DIR/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

"$PYTHON_BIN" -m unittest discover -s tests
"$PYTHON_BIN" -m compileall -q main.py davosbot scripts tests
