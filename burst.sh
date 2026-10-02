#!/usr/bin/env bash
# Usage: ADMIN_KEY=... ./burst.sh <BASE_URL> [--users N] [--concurrency C]
set -euo pipefail
uv run python burst/burst.py "$@"