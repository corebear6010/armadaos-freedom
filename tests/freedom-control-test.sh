#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$ROOT/decky/freedom-control/tests/sm8850-power-test.py"

echo "PASS: freedom-control-test"
