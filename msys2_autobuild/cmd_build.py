import os
import shutil
import sys
import time
import traceback
from typing import Any, List, Literal, Optional, Tuple

from .build import BuildError, build_package, run_cmd
from .config import BuildType, Config
from .gh import wait_for_api_limit_reset
from .queue import (Package, PackageStatus, get_buildqueue_with_status,
                    update_status)
from .utils import apply_optional_deps, gha_group

BuildFrom = Literal["start", "middle", "end"]


def get_package_to_build(
        pkgs: List[Package], build_types: Optional[List[BuildType]],
        build_from: BuildFrom) -> Optional[Tuple[Package, BuildType]]:

    can_build = []
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if build_types is not None and build_type not in build_types:
                continue
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                can_build.append((pkg, build_type))

    if not can_build:
        return None

    if build_from == "end":
        return can_build[-1]
    elif build_from == "middle":
        return can_build[len(can_build)//2]
    elif build_from == "start":
        return can_build[0]
    else:
        raise Exception("Unknown order:", build_from)


def run_build(args: Any) -> None:
    builddir = os.path.abspath(args.builddir)
    msys2_root = os.path.abspath(args.msys2_root)
    if args.build_types is None:
        build_types = None
    else:
        build_types = [p.strip() for p in args.build_types.split(",")]

    apply_optional_deps(args.optional_deps or "")

    start_time = time.monotonic()

    if not sys.platform == "win32":
        raise SystemExit("ERROR: Needs to run under native Python")

    if not shutil.which("git"):
        raise SystemExit("ERROR: git not in PATH")

    if not os.path.isdir(msys2_root):
        raise SystemExit("ERROR: msys2_root doesn't exist")

    try:
        run_cmd(msys2_root, [])
    except Exception as e:
        raise SystemExit("ERROR: msys2_root not functional", e)

    print(f"Building {build_types} starting from {args.build_from}")

    while True:
        wait_for_api_limit_reset()

        pkgs = get_buildqueue_with_status(full_details=True)
        update_status(pkgs)

        if (time.monotonic() - start_time) >= Config.SOFT_JOB_TIMEOUT:
            print("timeout reached")
            break

        todo = get_package_to_build(pkgs, build_types, args.build_from)
        if not todo:
            break
        pkg, build_type = todo

        try:
            with gha_group(f"[{ pkg['repo'] }] [{ build_type }] { pkg['name'] }..."):
                build_package(build_type, pkg, msys2_root, builddir)
        except BuildError:
            with gha_group(f"[{ pkg['repo'] }] [{ build_type }] { pkg['name'] }: failed"):
                traceback.print_exc(file=sys.stdout)
            continue


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser("build", help="Build all packages")
    sub.add_argument("-t", "--build-types", action="store")
    sub.add_argument(
        "--build-from", action="store", default="start", help="Start building from start|end|middle")
    sub.add_argument("--optional-deps", action="store")
    sub.add_argument("msys2_root", help="The MSYS2 install used for building. e.g. C:\\msys64")
    sub.add_argument(
        "builddir",
        help="A directory used for saving temporary build results and the git repos")
    sub.set_defaults(func=run_build)
