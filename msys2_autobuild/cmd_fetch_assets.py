import fnmatch
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import subprocess

from github.GitReleaseAsset import GitReleaseAsset

from .config import BuildType, Config
from .gh import (CachedAssets, download_asset, get_asset_filename,
                 get_asset_mtime_ns, is_asset_from_gha, get_asset_uploader_name)
from .queue import PackageStatus, get_buildqueue_with_status
from .utils import ask_yes_no


def get_repo_subdir(build_type: BuildType) -> Path:
    if build_type in Config.MSYS_ARCH_LIST:
        return Path("msys") / "x86_64"
    elif build_type == Config.MSYS_SRC_BUILD_TYPE:
        return Path("msys") / "sources"
    elif build_type == Config.MINGW_SRC_BUILD_TYPE:
        return Path("mingw") / "sources"
    elif build_type in Config.MINGW_ARCH_LIST:
        return Path("mingw") / build_type
    else:
        raise Exception("unknown type")


def fetch_assets(args: Any) -> None:
    target_dir = os.path.abspath(args.targetdir)
    fetch_all = args.fetch_all
    fetch_complete = args.fetch_complete

    all_patterns: dict[BuildType, list[str]] = {}
    all_blocked = []
    for pkg in get_buildqueue_with_status():
        for build_type in pkg.get_build_types():
            if args.build_type and build_type not in args.build_type:
                continue
            status = pkg.get_status(build_type)
            pkg_patterns = pkg.get_build_patterns(build_type)
            if status == PackageStatus.FINISHED:
                all_patterns.setdefault(build_type, []).extend(pkg_patterns)
            elif status in [PackageStatus.FINISHED_BUT_BLOCKED,
                            PackageStatus.FINISHED_BUT_INCOMPLETE]:
                if fetch_all or (fetch_complete and status != PackageStatus.FINISHED_BUT_INCOMPLETE):
                    all_patterns.setdefault(build_type, []).extend(pkg_patterns)
                else:
                    all_blocked.append(
                        (pkg["name"], build_type, pkg.get_status_details(build_type)))

    all_assets = {}
    cached_assets = CachedAssets()
    assets_to_download: dict[BuildType, list[GitReleaseAsset]] = {}
    for build_type, patterns in all_patterns.items():
        if build_type not in all_assets:
            all_assets[build_type] = cached_assets.get_assets(build_type)
        assets = all_assets[build_type]

        assets_mapping: dict[str, list[GitReleaseAsset]] = {}
        for asset in assets:
            assets_mapping.setdefault(get_asset_filename(asset), []).append(asset)

        for pattern in patterns:
            matches = fnmatch.filter(assets_mapping.keys(), pattern)
            if matches:
                found = assets_mapping[matches[0]]
                assets_to_download.setdefault(build_type, []).extend(found)

    to_fetch = {}
    for build_type, assets in assets_to_download.items():
        for asset in assets:
            asset_dir = Path(target_dir) / get_repo_subdir(build_type)
            asset_path = asset_dir / get_asset_filename(asset)
            to_fetch[str(asset_path)] = asset

    if not args.noconfirm:
        for path, asset in to_fetch.items():
            if not is_asset_from_gha(asset):
                if not ask_yes_no(f"WARNING: {get_asset_filename(asset)!r} is a manual upload "
                                  f"from {get_asset_uploader_name(asset)!r}, continue?"):
                    raise SystemExit("aborting")

    def file_is_uptodate(path: str, asset: GitReleaseAsset) -> bool:
        asset_path = Path(path)
        if not asset_path.exists():
            return False
        if asset_path.stat().st_size != asset.size:
            return False
        if get_asset_mtime_ns(asset) != asset_path.stat().st_mtime_ns:
            return False
        return True

    # find files that are either wrong or not what we want
    to_delete = []
    not_uptodate = []
    for root, dirs, files in os.walk(target_dir):
        for name in files:
            existing = os.path.join(root, name)
            if existing in to_fetch:
                asset = to_fetch[existing]
                if not file_is_uptodate(existing, asset):
                    to_delete.append(existing)
                    not_uptodate.append(existing)
            else:
                to_delete.append(existing)

    if args.delete and not args.pretend:
        # delete unwanted files
        for path in to_delete:
            os.remove(path)

        # delete empty directories
        for root, dirs, files in os.walk(target_dir, topdown=False):
            for name in dirs:
                path = os.path.join(root, name)
                if not os.listdir(path):
                    os.rmdir(path)

    # Finally figure out what to download
    todo = {}
    done = []
    for path, asset in to_fetch.items():
        if not os.path.exists(path) or path in not_uptodate:
            todo[path] = asset
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        else:
            done.append(path)

    if args.verbose and all_blocked:
        import pprint
        print("Packages that are blocked and why:")
        pprint.pprint(all_blocked)

    print(f"downloading: {len(todo)}, done: {len(done)} "
          f"blocked: {len(all_blocked)} (related builds missing)")

    print("Pass --verbose to see the list of blocked packages.")
    print("Pass --fetch-complete to also fetch blocked but complete packages")
    print("Pass --fetch-all to fetch all packages.")
    print("Pass --delete to clear the target directory")

    def verify_file(path: str, target: str) -> None:
        try:
            subprocess.run(["zstd", "--quiet", "--test", path], capture_output=True, check=True, text=True)
        except subprocess.CalledProcessError as e:
            raise Exception(f"zstd test failed for {target!r}: {e.stderr}") from e

    def fetch_item(item: tuple[str, GitReleaseAsset]) -> tuple[str, GitReleaseAsset]:
        asset_path, asset = item
        if not args.pretend:
            download_asset(asset, asset_path, verify_file)
        return item

    with ThreadPoolExecutor(8) as executor:
        for i, item in enumerate(executor.map(fetch_item, todo.items())):
            print(f"[{i + 1}/{len(todo)}] {get_asset_filename(item[1])}")

    print("done")


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
    sub.add_argument(
        "--delete", action="store_true", help="Clear targetdir of unneeded files")
    sub.add_argument(
        "--verbose", action="store_true", help="Show why things are blocked")
    sub.add_argument(
        "--pretend", action="store_true",
        help="Don't actually download, just show what would be done")
    sub.add_argument(
        "--fetch-all", action="store_true", help="Fetch all packages, even blocked ones")
    sub.add_argument(
        "--fetch-complete", action="store_true",
        help="Fetch all packages, even blocked ones, except incomplete ones")
    sub.add_argument(
        "-t", "--build-type", action="append",
        help="Only fetch packages for given build type(s) (may be used more than once)")
    sub.add_argument(
        "--noconfirm", action="store_true",
        help="Don't require user confirmation")
    sub.set_defaults(func=fetch_assets)
