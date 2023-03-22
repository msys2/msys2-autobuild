from typing import Dict, List, Literal, Tuple, Union

from urllib3.util import Retry

ArchType = Literal["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64", "msys"]
SourceType = Literal["mingw-src", "msys-src"]
BuildType = Union[ArchType, SourceType]


def get_all_build_types() -> List[BuildType]:
    all_build_types: List[BuildType] = ["msys", "msys-src", "mingw-src"]
    all_build_types.extend(Config.MINGW_ARCH_LIST)
    return all_build_types


REQUESTS_TIMEOUT = (15, 30)
REQUESTS_RETRY = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502])


class Config:

    ALLOWED_UPLOADERS = [
        ("User", "elieux"),
        ("User", "Alexpux"),
        ("User", "lazka"),
        ("User", "jeremyd2019"),
        ("Bot", "github-actions[bot]"),
    ]
    """Users that are allowed to upload assets. This is checked at download time"""

    MINGW_ARCH_LIST: List[ArchType] = ["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64"]
    """Arches we try to build"""

    MINGW_SRC_ARCH: ArchType = "ucrt64"
    """The arch that is used to build the source package (any mingw one should work)"""

    MAIN_REPO = "msys2/msys2-autobuild"
    """The path of this repo (used for accessing the assets)"""

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

    MANUAL_BUILD: List[Tuple[str, List[BuildType]]] = [
        ('mingw-w64-firebird-git', []),
        ('mingw-w64-qt5-static', ['mingw32', 'mingw64', 'ucrt64', 'clang32', 'clang64']),
        ('mingw-w64-qt6-static', ['mingw32', 'mingw64', 'ucrt64', 'clang32', 'clang64']),
        ('mingw-w64-arm-none-eabi-gcc', []),
    ]
    """Packages that take too long to build, or can't be build and should be handled manually"""

    IGNORE_RDEP_PACKAGES: List[str] = [
        'mingw-w64-qt5-static',
        'mingw-w64-qt6-static',
        'mingw-w64-zig',
    ]
    """XXX: These would in theory block rdeps, but no one fixed them, so we ignore them"""

    OPTIONAL_DEPS: Dict[str, List[str]] = {
        "mingw-w64-headers-git": ["mingw-w64-winpthreads-git", "mingw-w64-tools-git"],
        "mingw-w64-crt-git": ["mingw-w64-winpthreads-git"],
        "mingw-w64-clang": ["mingw-w64-libc++"],
    }
    """XXX: In case of cycles we mark these deps as optional"""
