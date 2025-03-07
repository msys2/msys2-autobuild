import glob
import os
from typing import Any

from .gh import get_release, get_repo_for_build_type, upload_asset
from .queue import PackageStatus, get_buildqueue_with_status


def upload_assets(args: Any) -> None:
    package_name = args.package
    src_dir = args.path
    src_dir = os.path.abspath(src_dir)

    pkgs = get_buildqueue_with_status()

    if package_name is not None:
        for pkg in pkgs:
            if pkg["name"] == package_name:
                break
        else:
            raise SystemExit(f"Package '{package_name}' not in the queue, check the 'show' command")
        pkgs = [pkg]

    pattern_entries = []
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)

            # ignore finished packages
            if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED,
                          PackageStatus.FINISHED_BUT_INCOMPLETE):
                continue

            pattern_entries.append((build_type, pkg.get_build_patterns(build_type)))

    print(f"Looking for the following files in {src_dir}:")
    for build_type, patterns in pattern_entries:
        for pattern in patterns:
            print("  ", pattern)

    matches = []
    for build_type, patterns in pattern_entries:
        for pattern in patterns:
            for match in glob.glob(os.path.join(src_dir, pattern)):
                matches.append((build_type, match))
    print(f"Found {len(matches)} files..")

    for build_type, match in matches:
        repo = get_repo_for_build_type(build_type)
        release = get_release(repo, 'staging-' + build_type)
        print(f"Uploading {match}")
        if not args.dry_run:
            upload_asset(release, match)
    print("Done")


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "upload-assets", help="Upload packages", allow_abbrev=False)
    sub.add_argument("path", help="Directory to look for packages in")
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be uploaded")
    sub.add_argument("-p", "--package", action="store", help=(
        "Only upload files belonging to a particualr package (pkgbase)"))
    sub.set_defaults(func=upload_assets)
