from __future__ import annotations

import concurrent.futures
import contextlib
import getpass
import hashlib
import http.server
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

from cursor_cli_manager.github_release import _default_fetch, download_and_install_release_bundle
from cursor_cli_manager.proxy import proxy_environment, proxy_urlopen, selected_proxy
from cursor_cli_manager.update import _default_runner


class _Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        assets = getattr(self.server, "fixture_assets", {})
        content = assets.get(self.path.split("?", 1)[0])
        if content is None:
            content = (self.path + "|" + (self.headers.get("Via") or "")).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _until(test, timeout=20):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        value = test()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition did not become true")


def _listens(port, child=None):
    if child is not None and child.poll() is not None:
        raise AssertionError("sshd exited during fixture setup")
    try:
        with socket.create_connection(("127.0.0.1", port), 0.1):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _terminal(command, env):
    import pty

    master, slave = pty.openpty()
    try:
        # A real foreground session matters for the production TTY check.
        launcher = (
            "import fcntl, os, sys, termios; "
            "fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
        )
        proc = subprocess.Popen([sys.executable, "-c", launcher, *command], start_new_session=True,
                                stdin=slave, stdout=slave, stderr=slave, env=env)
    finally:
        os.close(slave)
    output = bytearray()
    offset = 0

    def expect(marker, timeout=20):
        nonlocal offset
        deadline = time.monotonic() + timeout
        while output.find(marker, offset) == -1:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                raise AssertionError((marker, bytes(output)))
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                raise AssertionError((marker, bytes(output))) from exc
            if not data:
                raise AssertionError((marker, bytes(output)))
            output.extend(data)
        offset = output.index(marker, offset) + len(marker)

    try:
        yield proc, expect, lambda data: os.write(master, data)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        os.close(master)


@unittest.skipUnless(os.environ.get("CCM_PROXY_SSH_INTEGRATION") == "1", "isolated OpenSSH fixture is opt-in")
class ProxySSHIntegrationTest(unittest.TestCase):
    def test_parallel_start_forwarding_and_restart(self):
        ssh = shutil.which("ssh")
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        openssl = shutil.which("openssl")
        self.assertTrue(ssh and openssl and Path(sshd).is_file())
        with tempfile.TemporaryDirectory(prefix="ccm-ssh-") as work, contextlib.ExitStack() as cleanup:
            root = Path(work)
            env = dict(os.environ, CCM_PROXY_STATE_DIR=str(root / "state"), NO_PROXY="*")
            wrapper = root / "ssh"
            client_conf = root / "SSH config with spaces"
            ssh_pid = root / "ssh.pid"
            tmp = root / "scratch"
            tmp.mkdir()
            env["TMPDIR"] = str(tmp)
            wrapper.write_text(
                "#!/bin/sh\nif [ \"$1\" = '-N' ]; then printf '%s\\n' \"$$\" > {}; fi\nexec {} \"$@\"\n".format(
                    shlex.quote(str(ssh_pid)), shlex.quote(ssh)
                )
            )
            wrapper.chmod(0o700)
            env["PATH"] = str(root) + os.pathsep + env["PATH"]
            keys = [root / "host_key", root / "client_key"]
            for path in keys:
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
            host_key, client_key = keys
            port = _port()
            pubkey = (root / "host_key.pub").read_text().split()[:2]
            known_hosts = root / "known_hosts"
            known_hosts.write_text("[127.0.0.1]:{} {}\n".format(port, " ".join(pubkey)))
            included = root / "included SSH settings"
            included.write_text("Host via-jump\n HostName 127.0.0.1\n ProxyJump fixture-bastion\n")
            stalled = cleanup.enter_context(socket.socket())
            stalled.bind(("127.0.0.1", 0))
            stalled.listen()
            client_conf.write_text(
                'Include "{}"\nHost broken\n HostName 127.0.0.1\n Port {}\n'
                "Host stalled\n HostName 127.0.0.1\n Port {}\n"
                "Host fixture fixture-bastion\n HostName 127.0.0.1\n"
                "Match originalhost via-jump\n Port {}\n"
                "Host *\n Port {}\n User {}\n"
                " IdentityFile \"{}\"\n IdentitiesOnly yes\n UserKnownHostsFile \"{}\"\n"
                " GlobalKnownHostsFile /dev/null\n".format(
                    included, _port(), stalled.getsockname()[1], port, port, getpass.getuser(), client_key, known_hosts,
                )
            )
            server_conf = root / "sshd_config"
            server_conf.write_text(
                "ListenAddress 127.0.0.1\nPort {}\nHostKey {}\nPidFile {}\n"
                "AuthorizedKeysFile {}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
                "StrictModes no\nUsePAM {}\nAllowTcpForwarding yes\n".format(
                    port, host_key, root / "pid", root / "client_key.pub",
                    "yes" if os.environ.get("CCM_PROXY_TEST_SSHD_SUDO") == "1" else "no",
                )
            )
            prefix = ["sudo", "-n"] if os.environ.get("CCM_PROXY_TEST_SSHD_SUDO") == "1" else []
            logs = cleanup.enter_context((root / "sshd.log").open("w+"))
            def start_server():
                return subprocess.Popen(prefix + [sshd, "-D", "-e", "-f", str(server_conf)], stdout=logs, stderr=logs)

            server = start_server()

            def stop_server():
                if server.poll() is None:
                    if prefix:
                        subprocess.run(prefix + ["kill", "-TERM", (root / "pid").read_text().strip()], check=False)
                    else:
                        server.terminate()
                    server.wait(timeout=8)

            cleanup.callback(stop_server)
            try:
                _until(lambda: _listens(port, server), timeout=6)
            except AssertionError:
                logs.seek(0)
                self.fail(logs.read())

            def make_target(tls=None, assets=None, bind="127.0.0.1"):
                endpoint = cleanup.enter_context(http.server.ThreadingHTTPServer((bind, 0), _Target))
                endpoint.fixture_assets = assets or {}
                if tls:
                    endpoint.socket = tls.wrap_socket(endpoint.socket, server_side=True)
                thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
                thread.start()
                cleanup.callback(endpoint.shutdown)
                return endpoint.server_port

            http_port = make_target()
            cert, key = root / "target.crt", root / "target.key"
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj",
                 "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(str(cert), str(key))
            https_port = make_target(tls)

            command = [env["CCM_PROXY_TEST_CCM"]] if env.get("CCM_PROXY_TEST_CCM") else [
                sys.executable, "-m", "cursor_cli_manager",
            ]

            def ccm(*args, check=True):
                result = subprocess.run(
                    command + ["proxy", *args],
                    env=env, capture_output=True, text=True, timeout=65,
                )
                if check:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)
                return result

            def shutdown_profile():
                ccm("stop", "fixture", check=False)

            self._test_setup(command, env, root, client_conf, http_port, ssh_pid)
            ccm("profile", "add", "fixture", "--host", "fixture", "--ssh-config", str(client_conf),
                "--route-target", "127.0.0.1:{}".format(http_port))
            cleanup.callback(shutdown_profile)
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                starts = list(pool.map(lambda _: ccm("start", "fixture"), range(6)))
            first = starts[0]
            self.assertEqual(len({(r["url"], r["pid"], r["generation"]) for r in starts}), 1)
            if sys.platform.startswith("linux"):
                worker = Path("/proc") / str(first["pid"]) / "exe"
                self.assertEqual(worker.resolve().parents[1], root / "state" / "workers")

            with patch.dict(os.environ, env, clear=True), selected_proxy("fixture"):
                request = urllib.request.Request("http://localhost:{}/http".format(http_port))
                with proxy_urlopen(request, timeout=5) as response:
                    self.assertIn(b"/http|1.1 ccm-", response.read())
                request = urllib.request.Request("https://localhost:{}/https".format(https_port))
                with proxy_urlopen(request, timeout=5, context=ssl.create_default_context(cafile=str(cert))) as response:
                    self.assertEqual(response.read(), b"/https|")
                with self.assertRaises(urllib.error.URLError):
                    proxy_urlopen(request, timeout=5)
                missing = "http://127.0.0.1:{}/unavailable".format(_port())
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    proxy_urlopen(urllib.request.Request(missing), timeout=5)
                self.assertEqual(failed.exception.code, 502)
                revision = "1" * 40
                git_port = make_target(
                    assets={"/repo.git/info/refs": (revision + "\trefs/heads/main\n").encode()},
                    bind="127.0.0.2",
                )
                before_git = ccm("list")[0]["traffic"]["http"]
                code, out, err = _default_runner(
                    ["git", "ls-remote", "http://127.0.0.2:{}/repo.git".format(git_port)],
                    5,
                )
                self.assertEqual(code, 0, err)
                self.assertIn(revision, out)
                self.assertGreater(ccm("list")[0]["traffic"]["http"], before_git)
                if env.get("CCM_PROXY_TEST_WHEEL"):
                    wheel = Path(env["CCM_PROXY_TEST_WHEEL"])
                    wheel_data = wheel.read_bytes()
                    wheel_port = make_target(assets={"/" + wheel.name: wheel_data}, bind="127.0.0.2")
                    downloads = root / "downloads"
                    before_pip = ccm("list")[0]["traffic"]["http"]
                    code, _, err = _default_runner(
                        [sys.executable, "-m", "pip", "download", "--no-deps", "--no-index", "--no-cache-dir",
                         "--dest", str(downloads), "http://127.0.0.2:{}/{}".format(wheel_port, wheel.name)],
                        30,
                    )
                    self.assertEqual(code, 0, err)
                    self.assertEqual(hashlib.sha256((downloads / wheel.name).read_bytes()).digest(),
                                     hashlib.sha256(wheel_data).digest())
                    self.assertGreater(ccm("list")[0]["traffic"]["http"], before_pip)
                if os.environ.get("CCM_PROXY_TEST_CURSOR"):
                    cursor_env = proxy_environment(cursor=True)
                    counts = ccm("list")[0]["traffic"]
                    cursor = subprocess.run(
                        [os.environ["CCM_PROXY_TEST_CURSOR"], "models"], env=cursor_env, cwd=work,
                        capture_output=True, timeout=45,
                    )
                    after = ccm("list")[0]["traffic"]
                    message = (cursor.stdout + cursor.stderr).decode(errors="replace").lower()
                    categories = [
                        tag for tag in (
                            "not authenticated", "login", "network", "connect", "certificate", "tls",
                            "forbidden", "unauthorized", "timeout", "proxy", "failed", "error",
                        )
                        if tag in message
                    ]
                    self.assertGreater(after["connect"], counts["connect"], "Cursor did not complete a proxy CONNECT")
                    self.assertEqual(cursor.returncode, 0, "Cursor models failed ({})".format(", ".join(categories)))

            # A destination failure leaves the active SSH generation usable.
            self.assertEqual(ccm("list")[0]["generation"], first["generation"])
            os.kill(int(ssh_pid.read_text()), signal.SIGKILL)
            # A successful TCP handshake on every sample proves the public
            # listener is held throughout SSH-only recovery.
            def ssh_restarted():
                self.assertTrue(_listens(first["port"]))
                result = ccm("list")[0]
                if result["phase"] == "ready" and result["generation"] > first["generation"]:
                    return result
                return None

            recovered = _until(ssh_restarted)
            self.assertEqual(recovered["pid"], first["pid"])
            self.assertGreaterEqual(recovered["ssh_failures"], 1)
            stop_server()
            os.kill(int(ssh_pid.read_text()), signal.SIGKILL)

            def waiting_for_network():
                result = ccm("list")[0]
                return result if result["phase"] == "waiting-network" else None

            offline = _until(waiting_for_network)
            time.sleep(1.5)
            self.assertEqual(ccm("list")[0]["ssh_failures"], offline["ssh_failures"])
            self.assertTrue(_listens(first["port"]))
            server = start_server()
            _until(lambda: _listens(port, server), timeout=6)

            def recovered_from_offline():
                result = ccm("list")[0]
                return result if result["phase"] == "ready" and result["generation"] > offline["generation"] else None

            recovered = _until(recovered_from_offline)

            # A new helper invocation can recover a crashed gateway, and the
            # resident monitor also restarts it without another start command.
            os.kill(first["pid"], signal.SIGKILL)

            def restarted():
                result = ccm("list")[0]
                return result if result["verified"] and result["phase"] == "ready" and result["pid"] != first["pid"] else None

            second = _until(restarted)
            self.assertEqual(second["url"], first["url"])
            self.assertGreater(second["generation"], recovered["generation"])
            self.assertGreaterEqual(second["worker_failures"], 1)
            if env.get("CCM_PROXY_TEST_UPGRADE_BUNDLE"):
                asset_name = "ccm-fixture.tar.gz"
                data = Path(env["CCM_PROXY_TEST_UPGRADE_BUNDLE"]).read_bytes()
                asset_port = make_target(assets={
                    "/" + asset_name: data,
                    "/checksums.txt": "{}  {}\n".format(hashlib.sha256(data).hexdigest(), asset_name).encode(),
                })

                def fetch(url, timeout, headers):
                    fixture_url = "http://localhost:{}/{}".format(asset_port, url.rsplit("/", 1)[-1])
                    return _default_fetch(fixture_url, timeout, headers)

                with patch.dict(os.environ, env, clear=True), selected_proxy("fixture"):
                    installed = download_and_install_release_bundle(
                        repo="fixture/ccm", tag="v1.0.0", asset_name=asset_name,
                        install_root=root / "installed", bin_dir=root / "bin",
                        timeout_s=10, verify_checksums=True, fetch=fetch,
                    )
                new_exe = root / "bin" / "ccm"
                self.assertEqual(new_exe.resolve(), installed.resolve())
                result = subprocess.run(
                    [str(new_exe), "proxy", "list"], env=env, capture_output=True, text=True, timeout=10, check=True,
                )
                upgraded = json.loads(result.stdout)[0]
                self.assertEqual(upgraded["pid"], second["pid"])
                self.assertEqual(upgraded["url"], second["url"])
            ccm("stop", "fixture")
            self.assertFalse(_listens(first["port"]))
            self.assertEqual(ccm("start", "fixture")["url"], first["url"])
            ccm("reset", "fixture")
            foreground = subprocess.Popen(
                command + ["proxy", "start", "fixture", "--foreground"],
                env=env, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            with foreground:
                try:
                    _until(lambda: ccm("list")[0]["phase"] == "ready")
                    os.killpg(foreground.pid, signal.SIGINT)
                    _, stderr = foreground.communicate(timeout=15)
                    self.assertEqual(foreground.returncode, 130, stderr.decode(errors="replace"))
                    self.assertFalse(_listens(first["port"]))
                finally:
                    if foreground.poll() is None:
                        ccm("stop", "fixture", check=False)
            ccm("profile", "remove", "fixture")
            self.assertEqual(ccm("profile", "list"), [])

    def _test_setup(self, command, env, root, config, http_port, ssh_pid):
        state = root / "first-run-state"
        first = dict(env, CCM_PROXY_STATE_DIR=str(state))
        target = "127.0.0.1:{}".format(http_port)
        init = command + ["proxy", "init", "--ssh-config", str(config), "--route-target", target]

        for answer in (b"\x04", b"y\x04"):
            with _terminal(init, first) as (proc, expect, send):
                expect(b"Use an SSH proxy")
                send(answer)
                expect(b"setup canceled (input ended)")
                self.assertEqual(proc.wait(timeout=5), 1)
            self.assertFalse(state.exists())

        with _terminal(init, first) as (proc, expect, send):
            expect(b"Use an SSH proxy")
            send(b"y\n")
            expect(b"Choose an SSH connection")
            send(b"1\n")
            expect(b"SSH host is unreachable")
            expect(b"Choose an SSH connection")
            self.assertFalse(state.exists())
            send(b"\x03")
            self.assertEqual(proc.wait(timeout=5), 130)
        self.assertFalse(state.exists())

        last_pid = ssh_pid.read_text()
        with _terminal(init, first) as (proc, expect, send):
            expect(b"Use an SSH proxy")
            send(b"y\n")
            expect(b"Choose an SSH connection")
            send(b"2\n")
            _until(lambda: ssh_pid.read_text().strip() and ssh_pid.read_text() != last_pid, timeout=4)
            interrupted_pid = int(ssh_pid.read_text())
            send(b"\x03")
            self.assertEqual(proc.wait(timeout=5), 130)

        def processes_exited():
            try:
                os.kill(interrupted_pid, 0)
            except ProcessLookupError:
                return not list(Path(env["TMPDIR"]).glob("ccm-ssh-*"))
            return False

        _until(processes_exited, timeout=8)
        self.assertFalse(state.exists())

        with _terminal(init, first) as (proc, expect, send):
            expect(b"Use an SSH proxy")
            send(b"y\n")
            expect(b"Choose an SSH connection")
            send(b"a\n")
            expect(b"SSH config file")
            send(b"\n")
            expect(b"SSH config alias")
            send(b"via-jump\n")
            expect(b"Saved SSH proxy profile via-jump")
            self.assertEqual(proc.wait(timeout=8), 0)
        probe_pid = int(ssh_pid.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(probe_pid, 0)

        def run(*args):
            result = subprocess.run(command + list(args), env=first, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        status = json.loads(run("proxy", "profile", "list", "--setup"))
        self.assertEqual(status["mode"], "ssh")
        self.assertEqual([p["name"] for p in status["profiles"]], ["via-jump"])
        self.assertEqual(status["profiles"][0]["ssh_config"], str(config))
        self.assertEqual(status["id"], status["profiles"][0]["id"])
        replay = run("--config-dir", str(root / "cursor"), "open", "chat", "--workspace", str(root), "--dry-run")
        self.assertEqual(shlex.split(replay)[0], "ccm")
        self.assertFalse(json.loads(run("proxy", "list"))[0]["verified"])
        try:
            url = json.loads(run("proxy", "start", "via-jump"))["url"]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url}))
            with patch.dict(os.environ, {"no_proxy": "", "NO_PROXY": ""}):
                with opener.open("http://localhost:{}/setup".format(http_port), timeout=5) as response:
                    self.assertIn(b"/setup|1.1 ccm-", response.read())
        finally:
            run("proxy", "stop", "via-jump")
