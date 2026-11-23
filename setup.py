from pathlib import Path
import struct
import sys

from setuptools import Distribution, setup
from wheel.bdist_wheel import bdist_wheel


def bundled_proxy():
    helper = "ccm-proxy.exe" if sys.platform == "win32" else "ccm-proxy"
    return Path(__file__).resolve().parent / "cursor_cli_manager" / "bin" / helper


def macos_proxy_arch():
    with bundled_proxy().open("rb") as binary:
        header = binary.read(8)
    if len(header) == 8:
        magic, cpu_type = struct.unpack("<II", header)
        arch = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(cpu_type)
        if magic == 0xFEEDFACF and arch:
            return arch
    raise RuntimeError("the bundled macOS ccm-proxy must be a thin x86_64 or arm64 Mach-O binary")


class CCMDistribution(Distribution):
    def has_ext_modules(self):
        return bundled_proxy().is_file()


class CCMBinaryWheel(bdist_wheel):
    def get_tag(self):
        python_tag, abi_tag, platform_tag = super().get_tag()
        if platform_tag != "any":
            python_tag, abi_tag = "py3", "none"
            if platform_tag == "linux_x86_64":
                # The helper is a statically linked Go binary built with CGO disabled.
                platform_tag = "manylinux_2_17_x86_64"
            if platform_tag.startswith("macosx_"):
                _, major, minor, _ = platform_tag.split("_", 3)
                major, minor = max((int(major), int(minor)), (12, 0))
                # CPython may be universal2 even though the Go helper is thin.
                platform_tag = "macosx_{}_{}_{}".format(major, minor, macos_proxy_arch())
        return python_tag, abi_tag, platform_tag


setup(distclass=CCMDistribution, cmdclass={"bdist_wheel": CCMBinaryWheel})
