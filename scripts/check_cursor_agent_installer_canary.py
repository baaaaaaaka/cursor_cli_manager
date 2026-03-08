#!/usr/bin/env python3
from __future__ import annotations

from urllib.request import Request, urlopen

from cursor_cli_manager.cursor_agent_install import (
    fetch_official_installer_metadata,
    select_cursor_agent_install_spec,
)


def main() -> int:
    meta = fetch_official_installer_metadata()
    linux = select_cursor_agent_install_spec(meta, system="Linux", machine="x86_64")
    windows = select_cursor_agent_install_spec(meta, system="Windows", machine="AMD64")

    for url in (linux.download_url, windows.download_url):
        req = Request(url, method="HEAD", headers={"User-Agent": "cursor-cli-manager"})
        with urlopen(req, timeout=20) as resp:
            if resp.status not in (200, 301, 302):
                raise SystemExit(f"unexpected status {resp.status} for {url}")
            print(resp.status, url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
