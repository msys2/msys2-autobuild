import sys
import os
import argparse
import shutil
from os import environ
from github import Github
from urllib.request import urlopen
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
import time

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


def ensure_git_repo(url, path):
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])


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


def build_package(pkg, builddir):
    assert os.path.isabs(builddir)
    os.makedirs(builddir, exist_ok=True)
    assetdir_msys = os.path.join(builddir, "assets", "msys")
    os.makedirs(assetdir_msys, exist_ok=True)
    assetdir_mingw = os.path.join(builddir, "assets", "mingw")
    os.makedirs(assetdir_mingw, exist_ok=True)

    isMSYS = pkg['repo'][0:5] == 'MSYS2'
    repo_dir = os.path.join(builddir, pkg['repo'])
    pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
    ensure_git_repo(pkg['repo_url'], repo_dir)

    def run_cmd(args):
        check_call(['bash', '-c'] + [' '.join(args)], cwd=pkg_dir, timeout=get_timeout())

    try:
        run_cmd([
            'makepkg' if isMSYS else 'makepkg-mingw',
            '--noconfirm',
            '--noprogressbar',
            '--skippgpcheck',
            '--nocheck',
            '--syncdeps',
            '--rmdeps',
            '--cleanbuild'
        ])
        run_cmd([
            'makepkg',
            '--noconfirm',
            '--noprogressbar',
            '--skippgpcheck',
            '--allsource'
        ] + ([] if isMSYS else ['--config', '/etc/makepkg_mingw64.conf']))
    except subprocess.TimeoutExpired as e:
        raise BuildTimeoutError(e)
    except Exception as e:
        raise BuildError(e)
    else:
        for entry in os.listdir(pkg_dir):
            if fnmatch.fnmatch(entry, '*.pkg.tar.*') or fnmatch.fnmatch(entry, '*.src.tar.*'):
                shutil.move(os.path.join(pkg_dir, entry), assetdir_msys if isMSYS else assetdir_mingw)


def run_build(args):
    builddir = os.path.abspath(args.builddir)

    for pkg in get_packages_to_build()[2]:
        with gha_group(f"[{ pkg['repo'] }] { pkg['repo_path'] }..."):
            try:
                build_package(pkg, builddir)
            except BuildTimeoutError:
                print("timeout")
            except BuildError:
                print("failed")
                traceback.print_exc()


def get_packages_to_build():
    gh = Github(*get_credentials())

    repo = gh.get_repo('msys2/msys2-devtools')
    assets = []
    for name in ["msys", "mingw"]:
        assets.extend([a.name for a in repo.get_release('staging-' + name).get_assets()])

    tasks = []
    done = []

    for pkg in loads(urlopen("https://packages.msys2.org/api/buildqueue").read()):
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        isMSYS = pkg['repo'].startswith('MSYS2')
        isDone = True
        for item in pkg['packages']:
            if not fnmatch.filter(assets, '%s-%s-*.pkg.tar.*' % (
                item,
                pkg['version']
            )):
                isDone = False
                break
        if isDone:
            done.append(pkg)
        else:
            tasks.append(pkg)

    skipped = [pkg for pkg in tasks if pkg['repo_path'] in SKIP]
    todo = [pkg for pkg in tasks if pkg['repo_path'] not in SKIP]

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
            if asset_path.exists() and asset_path.stat().st_size == asset.size:
                skipped.append(asset)
                continue
            todo.append((asset, asset_path))

    print(f"downloading: {len(todo)}, skipped: {len(skipped)}")

    def fetch_item(item):
        with urlopen(item[0].browser_download_url, timeout=10) as conn:
            data = conn.read()
        return (item, data)

    with ThreadPoolExecutor(4) as executor:
        for i, (item, data) in enumerate(executor.map(fetch_item, todo)):
            print(f"[{i + 1}/{len(todo)}] {item[0].name}")
            with open(item[1], "wb") as h:
                h.write(data)

    print("done")


def get_credentials():
    if "GITHUB_TOKEN" in environ:
        return [environ["GITHUB_TOKEN"]]
    elif "GITHUB_USER" in environ and "GITHUB_PASS" in environ:
        return [environ["GITHUB_USER"], environ["GITHUB_PASS"]]
    else:
        raise Exception("'GITHUB_TOKEN' or 'GITHUB_USER'/'GITHUB_PASS' env vars not set")


def main(argv):
    parser = argparse.ArgumentParser(description="Build packages")
    parser.set_defaults(func=lambda *x: parser.print_help())
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("builddir")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser("show", help="Show all packages to be built")
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser("show-assets", help="Show all staging packages")
    sub.set_defaults(func=show_assets)

    sub = subparser.add_parser("fetch-assets", help="Download all staging packages")
    sub.add_argument("targetdir")
    sub.set_defaults(func=fetch_assets)

    get_credentials()

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)