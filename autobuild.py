import sys
import os
import argparse
from os import environ
from github import Github
from json import loads
from pathlib import Path
from subprocess import check_call
import subprocess
from sys import stdout
import fnmatch
import traceback
from tabulate import tabulate
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import requests
import shlex
import time
import tempfile

# After which overall time it should stop building (in seconds)
BUILD_TIMEOUT = 18000

# Packages that take too long to build, and should be handled manually
SKIP = [
    'mingw-w64-clang',
]


def timeoutgen(timeout):
    end = time.time() + timeout
    def new():
        return max(end - time.time(), 0)
    return new

get_timeout = timeoutgen(BUILD_TIMEOUT)


def get_repo_checkout_dir(repo):
    # some tools can't handle long paths, so try to have the build root near the disk root
    nick = ""
    if repo == "MINGW-packages":
        nick = "_MINGW"
    elif repo == "MSYS2-packages":
        nick = "_MSYS"
    else:
        raise ValueError("unknown repo: " + repo)

    if sys.platform == "msys":
        # root dir on the same drive
        win_path = subprocess.check_output(["cygpath", "-m", "/"], text=True).strip()
        posix_drive = subprocess.check_output(["cygpath", "-u", win_path[:3]], text=True).strip()
        return os.path.join(posix_drive, nick)
    else:
        raise NotImplementedError("fixme")


def ensure_git_repo(url, path):
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])
    else:
        check_call(["git", "fetch", "origin"], cwd=path)
        check_call(["git", "reset", "--hard", "origin/master"], cwd=path)


def reset_git_repo(path):
    assert os.path.exists(path)
    check_call(["git", "clean", "-xfdf"], cwd=path)
    check_call(["git", "reset", "--hard", "HEAD"], cwd=path)


@contextmanager
def gha_group(title):
    print(f'\n::group::{title}')
    stdout.flush()
    try:
        yield
    finally:
        print('::endgroup::')
        stdout.flush()


class BuildError(Exception):
    pass

class BuildTimeoutError(BuildError):
    pass


def upload_asset(type_: str, path: os.PathLike, replace=True):
    # type_: msys/mingw/failed
    path = Path(path)
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')
    release = repo.get_release("staging-" + type_)
    if replace:
        for asset in release.get_assets():
            if path.name == asset.name:
                asset.delete_asset()
    release.upload_asset(str(path))


def build_package(pkg, builddir):
    assert os.path.isabs(builddir)
    os.makedirs(builddir, exist_ok=True)

    isMSYS = pkg['repo'].startswith('MSYS2')
    repo_dir = get_repo_checkout_dir(pkg['repo'])
    pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
    ensure_git_repo(pkg['repo_url'], repo_dir)

    def run_cmd(args, **kwargs):
        check_call(['bash', '-c'] + [shlex.join(args)], cwd=pkg_dir, timeout=get_timeout(), **kwargs)

    makepkg = 'makepkg' if isMSYS else 'makepkg-mingw'

    try:
        run_cmd([
            makepkg,
            '--noconfirm',
            '--noprogressbar',
            '--skippgpcheck',
            '--nocheck',
            '--syncdeps',
            '--rmdeps',
            '--cleanbuild'
        ])

        env = environ.copy()
        if not isMSYS:
            env['MINGW_INSTALLS'] = 'mingw64'
        run_cmd([
            makepkg,
            '--noconfirm',
            '--noprogressbar',
            '--skippgpcheck',
            '--allsource'
        ], env=env)
    except subprocess.TimeoutExpired as e:
        raise BuildTimeoutError(e)
    except subprocess.CalledProcessError as e:

        for item in pkg['packages']:
            with tempfile.TemporaryDirectory() as tempdir:
                failed_path = os.path.join(tempdir, f"{item}-{pkg['version']}.failed")
                with open(failed_path, 'wb') as h:
                    # github doesn't allow empty assets
                    h.write(b'oh no')
                upload_asset("failed", failed_path)

        raise BuildError(e)
    else:
        for entry in os.listdir(pkg_dir):
            if fnmatch.fnmatch(entry, '*.pkg.tar.*') or fnmatch.fnmatch(entry, '*.src.tar.*'):
                path = os.path.join(pkg_dir, entry)
                upload_asset("msys" if isMSYS else "mingw", path)
    finally:
        # remove built artifacts
        reset_git_repo(repo_dir)


def run_build(args):
    builddir = os.path.abspath(args.builddir)

    for pkg in get_packages_to_build()[2]:
        try:
            with gha_group(f"[{ pkg['repo'] }] { pkg['repo_path'] }..."):
                build_package(pkg, builddir)
        except BuildTimeoutError:
            print("timeout")
            break
        except BuildError:
            print("failed")
            traceback.print_exc()


def get_buildqueue():
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue")
    r.raise_for_status()
    for pkg in r.json():
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)
    return pkgs


def get_packages_to_build():
    gh = Github(*get_credentials())

    repo = gh.get_repo('msys2/msys2-devtools')
    assets = []
    for name in ["msys", "mingw"]:
        assets.extend([a.name for a in repo.get_release('staging-' + name).get_assets()])
    assets_failed = [a.name for a in repo.get_release('staging-failed').get_assets()]

    def pkg_is_done(pkg):
        for item in pkg['packages']:
            if not fnmatch.filter(assets, f"{item}-{pkg['version']}-*.pkg.tar.*"):
                return False
        return True

    def pkg_has_failed(pkg):
        for item in pkg['packages']:
            if f"{item}-{pkg['version']}.failed" in assets_failed:
                return True
        return False

    todo = []
    done = []
    skipped = []
    for pkg in get_buildqueue():
        if pkg_is_done(pkg):
            done.append(pkg)
        elif pkg_has_failed(pkg) or pkg['repo_path'] in SKIP:
            skipped.append(pkg)
        else:
            todo.append(pkg)

    return done, skipped, todo


def show_build(args):
    done, skipped, todo = get_packages_to_build()

    def print_packages(title, pkgs):
        print()
        print(title)
        print(tabulate([(p["repo_path"], p["version"]) for p in pkgs], headers=["Package", "Version"]))

    print_packages("TODO:", todo)
    print_packages("SKIPPED:", skipped)
    print_packages("DONE:", done)


def show_assets(args):
    gh = Github(*get_credentials())

    for name in ["msys", "mingw"]:
        assets = gh.get_repo('msys2/msys2-devtools').get_release('staging-' + name).get_assets()

        print(tabulate(
            [[
                asset.name,
                asset.size,
                asset.created_at,
                asset.updated_at,
                #asset.browser_download_url
            ] for asset in assets],
            headers=["name", "size", "created", "updated"] #, "url"]
        ))


def fetch_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    todo = []

    def get_subdir(type_, entry):
        if type_ == "msys":
            if fnmatch.fnmatch(entry, '*.pkg.tar.*'):
                return "x86_64"
            elif fnmatch.fnmatch(entry, '*.src.tar.*'):
                return "sources"
            else:
                raise Exception("unknown file type")
        elif type_ == "mingw":
            if fnmatch.fnmatch(entry, '*.src.tar.*'):
                return "sources"
            elif entry.startswith("mingw-w64-x86_64-"):
                return "x86_64"
            elif entry.startswith("mingw-w64-i686-"):
                return "i686"
            else:
                raise Exception("unknown file type")

    skipped = []
    for name in ["msys", "mingw"]:
        p = Path(args.targetdir) / name
        assets = repo.get_release('staging-' + name).get_assets()
        for asset in assets:
            asset_dir = p / get_subdir(name, asset.name)
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path = asset_dir / asset.name
            if asset_path.exists():
                if asset_path.stat().st_size != asset.size:
                    print(f"Warning: {asset_path} already exists but has a different size")
                skipped.append(asset)
                continue
            todo.append((asset, asset_path))

    print(f"downloading: {len(todo)}, skipped: {len(skipped)}")

    def fetch_item(item):
        r = requests.get(item[0].browser_download_url, timeout=10)
        r.raise_for_status()
        return (item, r.content)

    with ThreadPoolExecutor(4) as executor:
        for i, (item, data) in enumerate(executor.map(fetch_item, todo)):
            print(f"[{i + 1}/{len(todo)}] {item[0].name}")
            with open(item[1], "wb") as h:
                h.write(data)

    print("done")


def trigger_gha_build(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')
    if repo.create_repository_dispatch('manual-build'):
        print("Build triggered")
    else:
        raise Exception("trigger failed")


def clean_gha_assets(args):
    gh = Github(*get_credentials())

    print("Fetching packages to build...")
    patterns = []
    for pkg in get_buildqueue():
        patterns.append(f"{pkg['name']}-{pkg['version']}*")
        for item in pkg['packages']:
            patterns.append(f"{item}-{pkg['version']}*")

    print("Fetching assets...")
    assets = {}
    repo = gh.get_repo('msys2/msys2-devtools')
    for release in ['staging-msys', 'staging-mingw', 'staging-failed']:
        for asset in repo.get_release(release).get_assets():
            assets.setdefault(asset.name, []).append(asset)

    for pattern in patterns:
        for key in fnmatch.filter(assets.keys(), pattern):
            del assets[key]

    for items in assets.values():
        for asset in items:
            print(f"Deleting {asset.name}...")
            if not args.dry_run:
                asset.delete_asset()

    if not assets:
        print("Nothing to delete")


def get_credentials():
    if "GITHUB_TOKEN" in environ:
        return [environ["GITHUB_TOKEN"]]
    elif "GITHUB_USER" in environ and "GITHUB_PASS" in environ:
        return [environ["GITHUB_USER"], environ["GITHUB_PASS"]]
    else:
        raise Exception("'GITHUB_TOKEN' or 'GITHUB_USER'/'GITHUB_PASS' env vars not set")


def main(argv):
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.set_defaults(func=lambda *x: parser.print_help())
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("builddir")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser("show", help="Show all packages to be built", allow_abbrev=False)
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser("show-assets", help="Show all staging packages", allow_abbrev=False)
    sub.set_defaults(func=show_assets)

    sub = subparser.add_parser("fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
    sub.set_defaults(func=fetch_assets)

    sub = subparser.add_parser("trigger", help="Trigger a GHA build", allow_abbrev=False)
    sub.set_defaults(func=trigger_gha_build)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument("--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    get_credentials()

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)