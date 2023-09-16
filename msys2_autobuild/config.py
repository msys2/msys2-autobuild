from typing import Dict, List, Literal, Tuple, Union

from urllib3.util import Retry

ArchType = Literal["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64", "msys"]
SourceType = Literal["mingw-src", "msys-src"]
BuildType = Union[ArchType, SourceType]


REQUESTS_TIMEOUT = (15, 30)
REQUESTS_RETRY = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502])


def get_all_build_types() -> List[BuildType]:
    all_build_types: List[BuildType] = []
    all_build_types.extend(Config.MSYS_ARCH_LIST)
    all_build_types.extend(Config.MINGW_ARCH_LIST)
    all_build_types.append(Config.MINGW_SRC_BUILD_TYPE)
    all_build_types.append(Config.MSYS_SRC_BUILD_TYPE)
    return all_build_types


def build_type_is_src(build_type: BuildType) -> bool:
    return build_type in [Config.MINGW_SRC_BUILD_TYPE, Config.MSYS_SRC_BUILD_TYPE]


class Config:

    ALLOWED_UPLOADERS = [
        "elieux",
        "lazka",
        "jeremyd2019",
    ]
    """Users that are allowed to upload assets. This is checked at download time"""

    MINGW_ARCH_LIST: List[ArchType] = ["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64"]
    """Arches we try to build"""

    MINGW_SRC_ARCH: ArchType = "ucrt64"
    """The arch that is used to build the source package (any mingw one should work)"""

    MINGW_SRC_BUILD_TYPE: BuildType = "mingw-src"

    MSYS_ARCH_LIST: List[ArchType] = ["msys"]

    MSYS_SRC_ARCH: ArchType = "msys"

    MSYS_SRC_BUILD_TYPE: BuildType = "msys-src"

    ASSETS_REPO: Dict[BuildType, str] = {
        "msys-src": "msys2/msys2-autobuild",
        "msys": "msys2/msys2-autobuild",
        "mingw-src": "msys2/msys2-autobuild",
        "mingw32": "msys2/msys2-autobuild",
        "mingw64": "msys2/msys2-autobuild",
        "ucrt64": "msys2/msys2-autobuild",
        "clang64": "msys2/msys2-autobuild",
        "clang32": "msys2/msys2-autobuild",
        "clangarm64": "msys2-arm/msys2-autobuild",
    }
    """Fetch certain build types from other repos if available"""

    SOFT_JOB_TIMEOUT = 60 * 60 * 3
    """Runtime after which we shouldn't start a new build"""

    MAXIMUM_JOB_COUNT = 15
    """Maximum number of jobs to spawn"""

    MAXIMUM_BUILD_TYPE_JOB_COUNT: Dict[BuildType, int] = {
        "msys-src": 1,
        "mingw-src": 1,
        "clangarm64": 1,
    }
    """Maximum jobs for each build type. Default is no limit."""

    MANUAL_BUILD: List[Tuple[str, List[BuildType]]] = [
        ('mingw-w64-firebird-git', []),
        ('mingw-w64-arm-none-eabi-gcc', []),
    ]
    """Packages that take too long to build, or can't be build and should be handled manually"""

    IGNORE_RDEP_PACKAGES: List[str] = [
        'mingw-w64-qt5-static',
        'mingw-w64-zig',
    ]
    """XXX: These would in theory block rdeps, but no one fixed them, so we ignore them"""

    OPTIONAL_DEPS: Dict[str, List[str]] = {
        "mingw-w64-headers-git": ["mingw-w64-winpthreads-git", "mingw-w64-tools-git"],
        "mingw-w64-crt-git": ["mingw-w64-winpthreads-git"],
        "mingw-w64-clang": ["mingw-w64-libc++"],
    }
    """XXX: In case of cycles we mark these deps as optional"""
