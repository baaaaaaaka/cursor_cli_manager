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
  echo "python3 or python is required to build the local fixture server" >&2
  exit 2
fi

BASE_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/ccm-auto-install.XXXXXX")"
FIXTURE_DIR="${BASE_DIR}/fixture"
INSTALL_ROOT="${BASE_DIR}/install-root"
BIN_DIR="${BASE_DIR}/install-bin"
CFG_DIR="${BASE_DIR}/config"
WS_DIR="${BASE_DIR}/workspace"
mkdir -p "${FIXTURE_DIR}" "${INSTALL_ROOT}" "${BIN_DIR}" "${CFG_DIR}" "${WS_DIR}"

VERSION="2026.02.27-e7d2ef6"
export FIXTURE_DIR OS_NAME ARCH_NAME VERSION
"${PYTHON_BIN}" - <<'PY'
import io
import os
import tarfile
from pathlib import Path

fixture = Path(os.environ["FIXTURE_DIR"])
os_name = os.environ["OS_NAME"]
arch = os.environ["ARCH_NAME"]
version = os.environ["VERSION"]

asset_dir = fixture / "lab" / version / os_name / arch
asset_dir.mkdir(parents=True, exist_ok=True)
asset_path = asset_dir / "agent-cli-package.tar.gz"

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    entries = {
        "dist-package/cursor-agent": b"#!/bin/sh\nexit 0\n",
        "dist-package/index.js": b"console.log('ok')\n",
        "dist-package/package.json": b"{}\n",
    }
    for name, data in entries.items():
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
asset_path.write_bytes(buf.getvalue())

install_sh = fixture / "install.sh"
install_sh.write_text(
    "#!/usr/bin/env bash\n"
    f'DOWNLOAD_URL="https://downloads.cursor.com/lab/{version}/${{OS}}/${{ARCH}}/agent-cli-package.tar.gz"\n',
    encoding="utf-8",
)
PY

PORT="$("${PYTHON_BIN}" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

"${PYTHON_BIN}" -m http.server "${PORT}" --bind 127.0.0.1 --directory "${FIXTURE_DIR}" >/tmp/ccm-auto-install-server.log 2>&1 &
SERVER_PID="$!"
cleanup() {
  kill "${SERVER_PID}" >/dev/null 2>&1 || true
  wait "${SERVER_PID}" >/dev/null 2>&1 || true
  rm -rf "${BASE_DIR}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
sleep 1

SANITIZED_PATH="/usr/bin:/bin:/usr/sbin:/sbin"
if [ ! -d /usr/sbin ]; then
  SANITIZED_PATH="/usr/bin:/bin"
fi

export CCM_CURSOR_AGENT_INSTALLER_URL="http://127.0.0.1:${PORT}/install.sh"
export CCM_CURSOR_AGENT_DOWNLOAD_BASE_URL="http://127.0.0.1:${PORT}"
export CCM_CURSOR_AGENT_INSTALL_ROOT="${INSTALL_ROOT}"
export CCM_CURSOR_AGENT_BIN_DIR="${BIN_DIR}"
export PATH="${SANITIZED_PATH}"
unset CURSOR_AGENT_PATH || true

"${CCM_BIN_PATH}" --config-dir "${CFG_DIR}" open abc123 --workspace "${WS_DIR}"

test -x "${INSTALL_ROOT}/versions/${VERSION}/cursor-agent"
test -e "${BIN_DIR}/cursor-agent"
