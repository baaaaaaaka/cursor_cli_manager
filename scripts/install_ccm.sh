#!/usr/bin/env sh
set -eu

# Install latest ccm release binary into ~/.local/bin (default).
#
# Customization via env vars:
# - CCM_GITHUB_REPO: "owner/name" (default: baaaaaaaka/cursor_cli_manager)
# - CCM_INSTALL_TAG: release tag like "v0.5.6" (default: latest)
# - CCM_INSTALL_DEST: install dir (default: ~/.local/bin)
# - CCM_INSTALL_FROM_DIR: local dir containing assets + checksums.txt (for offline/test)
# - CCM_INSTALL_OS / CCM_INSTALL_ARCH: override uname detection (for test)

REPO="${CCM_GITHUB_REPO:-baaaaaaaka/cursor_cli_manager}"
TAG="${CCM_INSTALL_TAG:-latest}"
DEST_DIR="${CCM_INSTALL_DEST:-${HOME}/.local/bin}"

OS="${CCM_INSTALL_OS:-$(uname -s)}"
ARCH="${CCM_INSTALL_ARCH:-$(uname -m)}"

case "$(printf '%s' "$ARCH" | tr '[:upper:]' '[:lower:]')" in
  x86_64|amd64) ARCH_NORM="x86_64" ;;
  aarch64|arm64) ARCH_NORM="arm64" ;;
  *) ARCH_NORM="$(printf '%s' "$ARCH" | tr '[:upper:]' '[:lower:]')" ;;
esac

ASSET=""
case "${OS}-${ARCH_NORM}" in
  Linux-x86_64) ASSET="ccm-linux-x86_64-glibc217" ;;
  Darwin-x86_64) ASSET="ccm-macos-x86_64" ;;
  Darwin-arm64) ASSET="ccm-macos-arm64" ;;
  *)
    printf '%s\n' "Unsupported platform: ${OS} ${ARCH}" 1>&2
    exit 2
    ;;
esac

mkdir -p "${DEST_DIR}"

# Create temp files inside DEST_DIR so final mv is atomic.
TMP_BIN="$(mktemp "${DEST_DIR%/}/.ccm.bin.XXXXXX")"
TMP_SUM="$(mktemp "${DEST_DIR%/}/.ccm.sums.XXXXXX")"
cleanup() {
  rm -f "${TMP_BIN}" "${TMP_SUM}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

fetch_to() {
  src="$1"
  out="$2"
  if [ -n "${CCM_INSTALL_FROM_DIR:-}" ]; then
    cp "${CCM_INSTALL_FROM_DIR%/}/${src}" "${out}"
    return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "${src}" -o "${out}"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "${out}" "${src}"
    return 0
  fi
  printf '%s\n' "Need curl or wget to download." 1>&2
  exit 3
}

if [ -n "${CCM_INSTALL_FROM_DIR:-}" ]; then
  fetch_to "${ASSET}" "${TMP_BIN}"
  if [ -f "${CCM_INSTALL_FROM_DIR%/}/checksums.txt" ]; then
    fetch_to "checksums.txt" "${TMP_SUM}"
  else
    : > "${TMP_SUM}"
  fi
else
  if [ "${TAG}" = "latest" ]; then
    BASE="https://github.com/${REPO}/releases/latest/download"
    fetch_to "${BASE}/${ASSET}" "${TMP_BIN}"
    # checksums are optional
    if fetch_to "${BASE}/checksums.txt" "${TMP_SUM}" 2>/dev/null; then
      :
    else
      : > "${TMP_SUM}"
    fi
  else
    BASE="https://github.com/${REPO}/releases/download/${TAG}"
    fetch_to "${BASE}/${ASSET}" "${TMP_BIN}"
    if fetch_to "${BASE}/checksums.txt" "${TMP_SUM}" 2>/dev/null; then
      :
    else
      : > "${TMP_SUM}"
    fi
  fi
fi

# Verify checksum if we have one.
if [ -s "${TMP_SUM}" ]; then
  EXPECTED="$(awk -v f="${ASSET}" '$NF==f {print $1; exit 0}' "${TMP_SUM}" | tr '[:upper:]' '[:lower:]' || true)"
  if [ -n "${EXPECTED}" ]; then
    ACTUAL=""
    if command -v sha256sum >/dev/null 2>&1; then
      ACTUAL="$(sha256sum "${TMP_BIN}" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')"
    elif command -v shasum >/dev/null 2>&1; then
      ACTUAL="$(shasum -a 256 "${TMP_BIN}" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')"
    fi
    if [ -n "${ACTUAL}" ] && [ "${ACTUAL}" != "${EXPECTED}" ]; then
      printf '%s\n' "Checksum mismatch for ${ASSET}: expected ${EXPECTED}, got ${ACTUAL}" 1>&2
      exit 4
    fi
  fi
fi

chmod 755 "${TMP_BIN}" || true

DEST="${DEST_DIR%/}/ccm"
# Atomic-ish replace (POSIX mv is atomic within same filesystem).
mv -f "${TMP_BIN}" "${DEST}"

# Provide the pip-style alias too (best-effort).
ALIAS="${DEST_DIR%/}/cursor-cli-manager"
(
  cd "${DEST_DIR%/}" || exit 0
  ln -sf "ccm" "$(basename "${ALIAS}")" 2>/dev/null || true
)

printf '%s\n' "Installed ${ASSET} -> ${DEST}"
printf '%s\n' "Alias: ${ALIAS} -> ${DEST}"
printf '%s\n' "Tip: ensure ${DEST_DIR} is on your PATH."

