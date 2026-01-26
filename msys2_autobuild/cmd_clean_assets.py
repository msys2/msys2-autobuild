import re
import fnmatch
from typing import Any

from github.GitReleaseAsset import GitReleaseAsset
from github.GitRelease import GitRelease

from .config import get_all_build_types
from .gh import (get_asset_filename, get_current_repo, get_release,
                 get_release_assets, make_writable)
from .queue import get_buildqueue


def get_assets_to_delete() -> list[GitReleaseAsset]:

    print("Fetching packages to build...")
    keep_patterns = []
    for pkg in get_buildqueue():
        for build_type in pkg.get_build_types():
            keep_patterns.append(pkg.get_failed_name(build_type))
            keep_patterns.extend(pkg.get_build_patterns(build_type))
    keep_pattern_regex = re.compile('|'.join(fnmatch.translate(p) for p in keep_patterns))

    def should_be_deleted(asset: GitReleaseAsset) -> bool:
        filename = get_asset_filename(asset)
        return not keep_pattern_regex.match(filename)

    def get_to_delete(release: GitRelease) -> list[GitReleaseAsset]:
        assets = get_release_assets(release, include_incomplete=True)
        to_delete = []
        for asset in assets:
            if should_be_deleted(asset):
                to_delete.append(asset)
        return to_delete

    def get_all_releases() -> list[GitRelease]:
        repo = get_current_repo()

        releases = []
        for build_type in get_all_build_types():
            releases.append(get_release(repo, "staging-" + build_type))
        releases.append(get_release(repo, "staging-failed"))
        return releases

    print("Fetching assets...")
    assets = []
    for release in get_all_releases():
        assets.extend(get_to_delete(release))

    return assets


def clean_gha_assets(args: Any) -> None:
    assets = get_assets_to_delete()

    print("Deleting assets...")
    for asset in assets:
        print(f"Deleting {get_asset_filename(asset)}...")
        if not args.dry_run:
            with make_writable(asset):
                asset.delete_asset()


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)
