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
#   ./out/ccm-linux-x86_64-glibc217

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT}/out"
ASSET_NAME="ccm-linux-x86_64-glibc217.tar.gz"

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
    python3 -m PyInstaller --clean -n ccm --add-data \"/opt/terminfo:terminfo\" --collect-data certifi --specpath out/_spec --distpath out/_dist --workpath out/_build cursor_cli_manager/__main__.py
    # Package the onedir output as a tarball for release distribution.
    tar -C out/_dist -czf out/${ASSET_NAME} ccm
  "

echo "Built ${OUT_DIR}/${ASSET_NAME}"

