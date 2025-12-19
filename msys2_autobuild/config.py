from typing import Literal, TypeAlias

from urllib3.util import Retry

ArchType = Literal["mingw32", "mingw64", "ucrt64", "clang64", "clangarm64", "msys"]
SourceType = Literal["mingw-src", "msys-src"]
BuildType: TypeAlias = ArchType | SourceType

REQUESTS_TIMEOUT = (15, 30)
REQUESTS_RETRY = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502])


def get_all_build_types() -> list[BuildType]:
    all_build_types: list[BuildType] = []
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

    MINGW_ARCH_LIST: list[ArchType] = ["mingw32", "mingw64", "ucrt64", "clang64", "clangarm64"]
    """Arches we try to build"""

    MINGW_SRC_ARCH: ArchType = "ucrt64"
    """The arch that is used to build the source package (any mingw one should work)"""

    MINGW_SRC_BUILD_TYPE: BuildType = "mingw-src"

    MSYS_ARCH_LIST: list[ArchType] = ["msys"]

    MSYS_SRC_ARCH: ArchType = "msys"

    MSYS_SRC_BUILD_TYPE: BuildType = "msys-src"

    RUNNER_CONFIG: dict[BuildType, dict] = {
        "msys-src": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
            "max_jobs": 1,
        },
        "msys": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
        },
        "mingw-src": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
            "max_jobs": 1,
        },
        "mingw32": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
        },
        "mingw64": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
        },
        "ucrt64": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
        },
        "clang64": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-2022"],
            "hosted": True,
        },
        "clangarm64": {
            "repo": "msys2/msys2-autobuild",
            "labels": ["windows-11-arm"],
            "hosted": True,
        },
    }
    """Runner config to use for each build type."""

    SOFT_JOB_TIMEOUT = 60 * 60 * 3
    """Runtime after which we shouldn't start a new build"""

    MAXIMUM_JOB_COUNT = 15
    """Maximum number of jobs to spawn"""

    MANUAL_BUILD: list[tuple[str, list[BuildType]]] = [
    ]
    """Packages that take too long to build, or can't be build and should be handled manually"""

    IGNORE_RDEP_PACKAGES: list[str] = [
    ]
    """XXX: These would in theory block rdeps, but no one fixed them, so we ignore them"""

    OPTIONAL_DEPS: dict[str, list[str]] = {
        "mingw-w64-headers": ["mingw-w64-winpthreads", "mingw-w64-tools"],
        "mingw-w64-crt": ["mingw-w64-winpthreads"],
        "mingw-w64-llvm": ["mingw-w64-libc++"],
    }
    """XXX: In case of cycles we mark these deps as optional"""
