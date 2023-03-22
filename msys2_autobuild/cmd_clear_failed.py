from typing import Any

from .gh import (get_asset_filename, get_main_repo, get_release,
                 get_release_assets, make_writable)
from .queue import get_buildqueue_with_status


def clear_failed_state(args: Any) -> None:
    build_type_filter = args.build_types
    build_type_list = build_type_filter.replace(" ", "").split(",") if build_type_filter else []
    package_filter = args.packages
    package_list = package_filter.replace(" ", "").split(",") if package_filter else []

    if build_type_filter is None and package_filter is None:
        raise SystemExit("clear-failed: At least one of --build-types or --packages needs to be passed")

    repo = get_main_repo()
    release = get_release(repo, 'staging-failed')
    assets_failed = get_release_assets(release)
    failed_map = dict((get_asset_filename(a), a) for a in assets_failed)

    for pkg in get_buildqueue_with_status():

        if package_filter is not None and pkg["name"] not in package_list:
            continue

        for build_type in pkg.get_build_types():
            if build_type_filter is not None and build_type not in build_type_list:
                continue

            name = pkg.get_failed_name(build_type)
            if name in failed_map:
                asset = failed_map[name]
                print(f"Deleting {get_asset_filename(asset)}...")
                if not args.dry_run:
                    with make_writable(asset):
                        asset.delete_asset()


def add_parser(subparsers) -> None:
    sub = subparsers.add_parser(
        "clear-failed", help="Clear the failed state for packages", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.add_argument("--build-types", action="store", help=(
        "A comma separated list of build types (e.g. mingw64)"))
    sub.add_argument("--packages", action="store", help=(
        "A comma separated list of packages to clear (e.g. mingw-w64-qt-creator)"))
    sub.set_defaults(func=clear_failed_state)
