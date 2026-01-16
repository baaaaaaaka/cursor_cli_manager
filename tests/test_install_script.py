import hashlib
import io
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


class TestInstallScript(unittest.TestCase):
    def _run_install(
        self, *, from_dir: Path, dest_dir: Path, root_dir: Path, checksums_ok: bool, tag: str = "latest"
    ) -> subprocess.CompletedProcess:
        asset = "ccm-linux-x86_64-glibc217.tar.gz"
        payload = b"fake-binary\n"
        tgz = from_dir / asset
        with tarfile.open(tgz, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="ccm/ccm")
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, fileobj=io.BytesIO(payload))
        sha = hashlib.sha256(tgz.read_bytes()).hexdigest()
        expected = sha if checksums_ok else ("0" * 64)
        (from_dir / "checksums.txt").write_text(f"{expected}  {asset}\n", encoding="utf-8")

        env = dict(os.environ)
        env.update(
            {
                "CCM_INSTALL_TAG": tag,
                "CCM_INSTALL_FROM_DIR": str(from_dir),
                "CCM_INSTALL_DEST": str(dest_dir),
                "CCM_INSTALL_ROOT": str(root_dir),
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
            root_dir = base / "root"
            from_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            p = self._run_install(from_dir=from_dir, dest_dir=dest_dir, root_dir=root_dir, checksums_ok=True)
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            out = dest_dir / "ccm"
            self.assertTrue(out.exists())
            self.assertTrue(out.is_symlink())
            expected = (root_dir / "versions" / "latest" / "ccm" / "ccm").resolve()
            self.assertEqual(out.resolve(), expected)
            self.assertTrue(expected.exists())
            self.assertTrue(expected.stat().st_mode & stat.S_IXUSR)
            self.assertEqual(expected.read_bytes(), b"fake-binary\n")
            alias = dest_dir / "cursor-cli-manager"
            self.assertTrue(alias.exists())
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(), out.resolve())

    def test_install_script_uses_explicit_tag_for_version_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            from_dir = base / "assets"
            dest_dir = base / "bin"
            root_dir = base / "root"
            from_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            p = self._run_install(
                from_dir=from_dir,
                dest_dir=dest_dir,
                root_dir=root_dir,
                checksums_ok=True,
                tag="v1.2.3",
            )
            self.assertEqual(p.returncode, 0, msg=p.stderr)
            expected = (root_dir / "versions" / "v1.2.3" / "ccm" / "ccm").resolve()
            self.assertTrue(expected.exists())
            self.assertEqual((dest_dir / "ccm").resolve(), expected)

    def test_install_script_fails_on_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            from_dir = base / "assets"
            dest_dir = base / "bin"
            root_dir = base / "root"
            from_dir.mkdir(parents=True, exist_ok=True)
            dest_dir.mkdir(parents=True, exist_ok=True)

            p = self._run_install(from_dir=from_dir, dest_dir=dest_dir, root_dir=root_dir, checksums_ok=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("Checksum mismatch", p.stderr)
            self.assertFalse((dest_dir / "ccm").exists())
            self.assertFalse((dest_dir / "cursor-cli-manager").exists())


if __name__ == "__main__":
    unittest.main()

