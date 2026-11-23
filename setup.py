from pathlib import Path
import sys

from setuptools import Distribution, setup
from wheel.bdist_wheel import bdist_wheel


class CCMDistribution(Distribution):
    def has_ext_modules(self):
        helper = "ccm-proxy.exe" if sys.platform == "win32" else "ccm-proxy"
        return (Path(__file__).resolve().parent / "cursor_cli_manager" / "bin" / helper).is_file()


class CCMBinaryWheel(bdist_wheel):
    def get_tag(self):
        python_tag, abi_tag, platform_tag = super().get_tag()
        if platform_tag != "any":
            python_tag, abi_tag = "py3", "none"
            if platform_tag == "linux_x86_64":
                # The helper is a statically linked Go binary built with CGO disabled.
                platform_tag = "manylinux_2_17_x86_64"
            if platform_tag.startswith("macosx_"):
                _, major, minor, arch = platform_tag.split("_", 3)
                if (int(major), int(minor)) < (12, 0):
                    platform_tag = "macosx_12_0_" + arch
        return python_tag, abi_tag, platform_tag


setup(distclass=CCMDistribution, cmdclass={"bdist_wheel": CCMBinaryWheel})
