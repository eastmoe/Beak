from setuptools import setup
from sysconfig import get_platform

try:
    from setuptools.command.bdist_wheel import bdist_wheel
except ModuleNotFoundError:
    from wheel.bdist_wheel import bdist_wheel


class PlatformWheel(bdist_wheel):
    def get_tag(self) -> tuple[str, str, str]:
        platform = (self.plat_name or get_platform()).replace("-", "_").replace(".", "_")
        return "py3", "none", platform


setup(cmdclass={"bdist_wheel": PlatformWheel})
