from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlsplit

_PROFILE = ContextVar("ccm_proxy_profile", default=(None, None))
_MANAGED_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


class ProxyError(RuntimeError):
    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


class ProxyHelperMissing(ProxyError):
    pass


@contextmanager
def selected_proxy(name: Optional[str], profile_id: Optional[str] = None) -> Iterator[None]:
    token = _PROFILE.set((name, profile_id))
    try:
        yield
    finally:
        _PROFILE.reset(token)


def selected_profile() -> Optional[str]:
    return _PROFILE.get()[0]


def selected_profile_id() -> Optional[str]:
    return _PROFILE.get()[1]


def _state_dir() -> Path:
    override = os.environ.get("CCM_PROXY_STATE_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ProxyError("CCM_PROXY_STATE_DIR must be absolute")
        return path
    if sys.platform.startswith("win"):
        configured = os.environ.get("LOCALAPPDATA")
        default = Path.home() / "AppData" / "Local"
    else:
        configured = os.environ.get("XDG_STATE_HOME")
        default = Path.home() / ".local" / "state"
    root = Path(configured) if configured and Path(configured).is_absolute() else default
    return root / "ccm" / "proxy"


def _helper() -> Path:
    override = os.environ.get("CCM_PROXY_HELPER")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ProxyError("CCM_PROXY_HELPER must identify an existing executable by absolute path")
        return path
    name = "ccm-proxy.exe" if sys.platform.startswith("win") else "ccm-proxy"
    if getattr(sys, "frozen", False):
        path = Path(sys.executable).resolve().with_name(name)
    else:
        path = Path(__file__).resolve().parent / "bin" / name
    if path.is_file():
        return path
    fallback = _state_dir() / "compat-v1" / name
    if not getattr(sys, "frozen", False) and fallback.is_file():
        return fallback
    raise ProxyHelperMissing("ccm-proxy is missing. Install the complete CCM bundle or build the helper with scripts/build_proxy.py.")


def preserve_helper_for_vcs_update() -> None:
    if selected_profile() is None:
        return
    source = _helper()
    fallback = _state_dir() / "compat-v1" / ("ccm-proxy.exe" if sys.platform.startswith("win") else "ccm-proxy")
    if fallback.is_file():
        return
    try:
        fallback.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".helper-", dir=str(fallback.parent)) as scratch:
            staged = Path(scratch) / fallback.name
            shutil.copy2(str(source), str(staged))
            staged.replace(fallback)
    except OSError as exc:
        if not fallback.is_file():
            raise ProxyError("could not preserve the proxy helper for the VCS upgrade: {}".format(exc)) from exc


def _native_environment(base: Mapping[str, str]) -> Dict[str, str]:
    env = dict(base)
    if not getattr(sys, "frozen", False):
        return env
    if sys.platform.startswith("linux"):
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        key = next((k for k in env if k.upper() == "PATH"), "PATH")
        for name in (key, "DYLD_LIBRARY_PATH"):
            if name in env:
                prefix = os.path.normcase(os.path.abspath(bundle))
                entries = env[name].split(os.pathsep)
                env[name] = os.pathsep.join(
                    path for path in entries
                    if not (os.path.normcase(os.path.abspath(path)) + os.sep).startswith(prefix + os.sep)
                )
    return env


def _call(args: Sequence[str], *, foreground: bool = False) -> object:
    command = [str(_helper()), "--state-dir", str(_state_dir()), *args]
    environment = _native_environment(os.environ)
    try:
        if foreground:
            with subprocess.Popen(command, stdout=subprocess.PIPE, env=environment) as child:
                try:
                    stdout, _ = child.communicate()
                except KeyboardInterrupt:
                    try:
                        _call(["stop", args[1]])
                    finally:
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                    raise SystemExit(130)
                proc = subprocess.CompletedProcess(command, child.returncode, stdout)
        else:
            proc = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProxyError("could not run ccm-proxy: {}".format(exc)) from exc
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (ValueError, AttributeError) as exc:
        raise ProxyError("ccm-proxy returned an invalid response") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ProxyError("ccm-proxy uses an unsupported interface version")
    if proc.returncode or data.get("error"):
        raise ProxyError(str(data.get("error") or "ccm-proxy failed"), code=data.get("code"))
    return data.get("result")


def command(args: Sequence[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(
            "ccm proxy init [--ssh-config FILE] [--route-target HOST:PORT]\n"
            "ccm proxy profile add NAME --host HOST [--ssh-config FILE | --user USER --port PORT] [--route-target HOST:PORT]\n"
            "ccm proxy profile list\n"
            "ccm proxy profile remove NAME\n"
            "ccm proxy start NAME [--foreground]\n"
            "ccm proxy list\n"
            "ccm proxy stop NAME\n"
            "ccm proxy prune\n"
            "ccm proxy reset NAME\n"
            "ccm proxy doctor NAME"
        )
        return 0
    if args[0] == "init":
        from cursor_cli_manager.proxy_setup import init_command

        return init_command(args[1:])
    if args[0] not in {"profile", "start", "list", "stop", "prune", "reset", "doctor"}:
        raise ProxyError("unknown proxy command: {}".format(args[0]))
    result = _call(args, foreground=args[0] == "start" and "--foreground" in args)
    if result is not None:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def _managed_url() -> Optional[str]:
    name, profile_id = _PROFILE.get()
    if name is None:
        return None
    result = _call(["ensure", name, profile_id] if profile_id else ["ensure", name])
    url = result.get("url") if isinstance(result, dict) and result.get("verified") is True else None
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        if (
            parts and parts.scheme == "http" and parts.hostname == "127.0.0.1"
            and parts.port and 1 <= parts.port <= 65535
            and parts.netloc == "127.0.0.1:{}".format(parts.port)
            and not (parts.path or parts.query or parts.fragment)
        ):
            return url
    except ValueError:
        pass
    raise ProxyError("ccm-proxy did not return a verified local HTTP endpoint")


def _no_proxy(env: Mapping[str, str]) -> str:
    entries = ["localhost", "127.0.0.1", "::1"]
    for item in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(","):
        value = item.strip().lower()
        try:
            loopback = ipaddress.ip_address(value.strip("[]")).is_loopback
        except ValueError:
            loopback = value == "localhost" or value.endswith(".localhost")
        if loopback and value not in entries:
            entries.append(value)
    return ",".join(entries)


def proxy_environment(
    base: Optional[Mapping[str, str]] = None,
    *,
    cursor: bool = False,
    package_manager: bool = False,
) -> Optional[Dict[str, str]]:
    url = _managed_url()
    if url is None:
        return None
    original = os.environ if base is None else base
    env = _native_environment({k: v for k, v in original.items() if k.upper() not in _MANAGED_KEYS})
    values = {"HTTP_PROXY": url, "HTTPS_PROXY": url, "ALL_PROXY": url, "NO_PROXY": _no_proxy(original)}
    env.update(values)
    if not sys.platform.startswith("win"):
        env.update({k.lower(): v for k, v in values.items()})
    if cursor:
        env["NODE_USE_ENV_PROXY"] = "1"
    if package_manager:
        env["PIP_PROXY"] = url
        env["GIT_ALLOW_PROTOCOL"] = "http:https"
        try:
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            if not 0 <= count <= 1000:
                raise ValueError()
        except ValueError as exc:
            raise ProxyError("invalid GIT_CONFIG_COUNT") from exc
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        env["GIT_CONFIG_KEY_{}".format(count)] = "http.proxy"
        env["GIT_CONFIG_VALUE_{}".format(count)] = url
    return env


def proxy_subprocess_options(*, cursor: bool = False, package_manager: bool = False) -> dict:
    env = proxy_environment(cursor=cursor, package_manager=package_manager)
    return {} if env is None else {"env": env}


class _ManagedProxyHandler(urllib.request.ProxyHandler):
    def proxy_open(self, req, proxy, type):
        # The configured loopback gateway is already verified by the helper.
        # Do not let urllib consult ambient no_proxy or registry proxy bypasses.
        req.set_proxy(urlsplit(proxy).netloc, "http")
        return None


class _ManagedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            raise ProxyError("a managed proxy requires an HTTP or HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def proxy_urlopen(req, *, timeout: float, context=None):
    url = _managed_url()
    if url is None:
        options = {"context": context} if context is not None else {}
        return urllib.request.urlopen(req, timeout=timeout, **options)
    if req.type not in ("http", "https"):
        raise ProxyError("a managed proxy requires an HTTP or HTTPS URL")
    req = urllib.request.Request(req.full_url, data=req.data, headers=dict(req.header_items()), method=req.get_method())
    handlers = [_ManagedProxyHandler({"http": url, "https": url}), _ManagedRedirectHandler()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)
