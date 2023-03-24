import fnmatch
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from github.GitReleaseAsset import GitReleaseAsset

from .config import get_all_build_types
from .gh import (get_asset_filename, get_current_repo, get_release,
                 get_release_assets, make_writable)
from .queue import get_buildqueue


def get_assets_to_delete() -> List[GitReleaseAsset]:
    repo = get_current_repo()

    print("Fetching packages to build...")
    patterns = []
    for pkg in get_buildqueue():
        for build_type in pkg.get_build_types():
            patterns.append(pkg.get_failed_name(build_type))
            patterns.extend(pkg.get_build_patterns(build_type))

    print("Fetching assets...")
    assets: Dict[str, List[GitReleaseAsset]] = {}
    for build_type in get_all_build_types():
        release = get_release(repo, "staging-" + build_type)
        for asset in get_release_assets(release, include_incomplete=True):
            assets.setdefault(get_asset_filename(asset), []).append(asset)

    release = get_release(repo, "staging-failed")
    for asset in get_release_assets(release, include_incomplete=True):
        assets.setdefault(get_asset_filename(asset), []).append(asset)

    for pattern in patterns:
        for key in fnmatch.filter(assets.keys(), pattern):
            del assets[key]

    result = []
    for items in assets.values():
        for asset in items:
            result.append(asset)
    return result


def clean_gha_assets(args: Any) -> None:
    assets = get_assets_to_delete()

    def delete_asset(asset: GitReleaseAsset) -> None:
        print(f"Deleting {get_asset_filename(asset)}...")
        if not args.dry_run:
            with make_writable(asset):
                asset.delete_asset()

    with ThreadPoolExecutor(4) as executor:
        for item in executor.map(delete_asset, assets):
            pass


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)
