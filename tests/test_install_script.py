import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestInstallScript(unittest.TestCase):
    def _run_install(self, *, from_dir: Path, dest_dir: Path, checksums_ok: bool) -> subprocess.CompletedProcess:
        asset = "ccm-linux-x86_64-glibc217"
        payload = b"fake-binary\n"
        (from_dir / asset).write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        expected = sha if checksums_ok else ("0" * 64)
        (from_dir / "checksums.txt").write_text(f"{expected}  {asset}\n", encoding="utf-8")

        env = dict(os.environ)
        env.update(
            {
                "CCM_INSTALL_FROM_DIR": str(from_dir),
                "CCM_INSTALL_DEST": str(dest_dir),
                "CCM_INSTALL_OS": "Linux",
                "CCM_INSTALL_ARCH": "x86_64",
            }
        )
        p = subprocess.run(
            ["sh", "scripts/install_ccm.sh"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return p

    def test_install_script_installs_binary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            from_dir = base / "assets"
            dest_dir = base / "bin"
            from_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            p = self._run_install(from_dir=from_dir, dest_dir=dest_dir, checksums_ok=True)
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            out = dest_dir / "ccm"
            self.assertTrue(out.exists())
            self.assertTrue(out.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(out.read_bytes(), b"fake-binary\n")

    def test_install_script_fails_on_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            from_dir = base / "assets"
            dest_dir = base / "bin"
            from_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            p = self._run_install(from_dir=from_dir, dest_dir=dest_dir, checksums_ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("Checksum mismatch", p.stderr)
            self.assertFalse((dest_dir / "ccm").exists())


if __name__ == "__main__":
    unittest.main()

