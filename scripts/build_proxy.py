"""Build the standalone CCM proxy for a source install or an onedir bundle."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "cursor_cli_manager" / "bin")
    parser.add_argument("--goos", choices=("linux", "darwin", "windows"))
    parser.add_argument("--goarch", choices=("amd64", "arm64"))
    args = parser.parse_args()
    env = dict(os.environ, CGO_ENABLED="0")
    if args.goos:
        env["GOOS"] = args.goos
    if args.goarch:
        env["GOARCH"] = args.goarch
    target_os = env.get("GOOS") or ("windows" if sys.platform == "win32" else sys.platform)
    name = "ccm-proxy.exe" if target_os == "windows" else "ccm-proxy"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ccm-proxy-", dir=str(output)) as work:
        binary = Path(work) / name
        subprocess.run(
            ["go", "build", "-trimpath", "-o", str(binary), "."],
            cwd=str(ROOT / "proxy"),
            env=env,
            check=True,
        )
        binary.replace(output / name)
    print(output / name)


if __name__ == "__main__":
    main()
