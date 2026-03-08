#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <linux|darwin> <x64|arm64>" >&2
  exit 2
fi

OS_NAME="$1"
ARCH_NAME="$2"
CCM_BIN_PATH="${CCM_BIN:-}"

if [ -z "${CCM_BIN_PATH}" ]; then
  echo "CCM_BIN must point to the built ccm executable" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"
if [ -z "${PYTHON_BIN}" ]; then
  echo "python3 or python is required to run the local fixture server" >&2
  exit 2
fi

exec "${PYTHON_BIN}" scripts/test_auto_install_bundle.py \
  --os "${OS_NAME}" \
  --arch "${ARCH_NAME}" \
  --ccm-bin "${CCM_BIN_PATH}"
