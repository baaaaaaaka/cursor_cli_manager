#!/usr/bin/env bash
set -euo pipefail

# Build the Linux release binary inside a manylinux2014-based builder image.
#
# Default image is a GHCR-hosted builder that already contains:
# - OpenSSL (for ssl-enabled Python)
# - shared CPython (PyInstaller requires shared libpython)
# - PyInstaller
#
# Override image:
#   CCM_LINUX_BUILDER_IMAGE=ccm-linux-builder:local ./scripts/build_linux_binary_docker.sh
#
# Output:
#   ./out/ccm-linux-x86_64-glibc217.tar.gz
#   ./out/ccm-linux-x86_64-nc5.tar.gz
#   ./out/ccm-linux-x86_64-nc6.tar.gz

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT}/out"
VARIANT="${CCM_LINUX_VARIANT:-common}"
ASSET_NAME="${CCM_LINUX_ASSET_NAME:-}"
TERMINFO_DIR="${CCM_LINUX_TERMINFO_DIR:-/opt/terminfo}"

if [ -z "${ASSET_NAME}" ]; then
  case "${VARIANT}" in
    nc5) ASSET_NAME="ccm-linux-x86_64-nc5.tar.gz" ;;
    nc6) ASSET_NAME="ccm-linux-x86_64-nc6.tar.gz" ;;
    *) ASSET_NAME="ccm-linux-x86_64-glibc217.tar.gz" ;;
  esac
fi

IMAGE="${CCM_LINUX_BUILDER_IMAGE:-ghcr.io/baaaaaaaka/ccm-linux-builder:py311}"

mkdir -p "${OUT_DIR}"

if [[ "${IMAGE}" != *":local"* ]] && [[ "${IMAGE}" != ccm-linux-builder:* ]]; then
  # Best-effort pull (ok if already present).
  docker pull "${IMAGE}" >/dev/null 2>&1 || true
fi

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "${ROOT}:/work" \
  -w /work \
  -e HOME=/tmp \
  -e PYINSTALLER_CONFIG_DIR=/tmp/pyinstaller \
  "${IMAGE}" \
  /bin/bash -lc "
    set -euxo pipefail
    cd /work
    python3 -m PyInstaller --clean -n ccm --hidden-import _curses --add-data \"${TERMINFO_DIR}:terminfo\" --collect-data certifi --specpath out/_spec --distpath out/_dist --workpath out/_build cursor_cli_manager/__main__.py
    python3 -c 'from pathlib import Path; import sys; base=Path(\"out/_dist/ccm/_internal\"); ver=f\"python{sys.version_info[0]}.{sys.version_info[1]}\"; dyn=base/ver/\"lib-dynload\"; mods=list(dyn.glob(\"_curses*.so\")); sys.exit(0 if mods else (sys.stderr.write(\"missing _curses in bundle\\n\") or 1))'
    python3 -c 'from pathlib import Path; import sys; base=Path(\"out/_dist/ccm/_internal\"); want=sys.argv[1] if len(sys.argv) > 1 else \"\"; ok=True; ok=((base/\"libtinfo.so.6\").exists() or (base/\"libncursesw.so.6\").exists()) if want==\"nc6\" else ok; ok=((base/\"libtinfo.so.5\").exists() or (base/\"libncursesw.so.5\").exists()) if want==\"nc5\" else ok; (want in (\"nc5\",\"nc6\")) and (not ok) and (sys.stderr.write(f\"missing ncurses libs for {want} in bundle\\n\") or True) and sys.exit(1)' \"${VARIANT}\"
    # Package the onedir output as a tarball for release distribution.
    tar -C out/_dist -czf out/${ASSET_NAME} ccm
  "

echo "Built ${OUT_DIR}/${ASSET_NAME}"

