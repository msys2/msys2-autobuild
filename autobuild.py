import sys
import os
import argparse
from os import environ
from github import Github
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
import shutil

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


def run_cmd(args, **kwargs):
    check_call(['bash', '-c'] + [shlex.join(args)], **kwargs)


@contextmanager
def fresh_git_repo(url, path):
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])
    else:
        check_call(["git", "fetch", "origin"], cwd=path)
        check_call(["git", "reset", "--hard", "origin/master"], cwd=path)
    try:
        yield
    finally:
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


class MissingDependencyError(BuildError):
    pass


class BuildTimeoutError(BuildError):
    pass


def download_asset(asset, target_path: str, timeout=15) -> str:
    with requests.get(asset.browser_download_url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(target_path, 'wb') as h:
            for chunk in r.iter_content(4096):
                h.write(chunk)


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


@contextmanager
def backup_pacman_conf():
    shutil.copyfile("/etc/pacman.conf", "/etc/pacman.conf.backup")
    try:
        yield
    finally:
        os.replace("/etc/pacman.conf.backup", "/etc/pacman.conf")


@contextmanager
def staging_dependencies(pkg, builddir):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    def add_to_repo(repo_root, repo_type, asset):
        repo_dir = Path(repo_root) / get_repo_subdir(repo_type, asset)
        os.makedirs(repo_dir, exist_ok=True)
        print(f"Downloading {asset.name}...")
        package_path = os.path.join(repo_dir, asset.name)
        download_asset(asset, package_path)

        repo_name = "autobuild-" + (
            str(get_repo_subdir(repo_type, asset)).replace("/", "-").replace("\\", "-"))
        repo_db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")

        with open("/etc/pacman.conf", "r", encoding="utf-8") as h:
            text = h.read()
            if str(repo_dir) not in text:
                print(repo_dir)
                text.replace("#RemoteFileSigLevel = Required",
                             "RemoteFileSigLevel = Never")
                with open("/etc/pacman.conf", "w", encoding="utf-8") as h2:
                    h2.write(f"""[{repo_name}]
Server={Path(repo_dir).as_uri()}
SigLevel=Never
""")
                    h2.write(text)

        run_cmd(["repo-add", repo_db_path, package_path], cwd=repo_dir)

    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with backup_pacman_conf():
            to_add = []
            for name, dep in pkg['ext-depends'].items():
                pattern = f"{name}-{dep['version']}-*.pkg.*"
                repo_type = "msys" if dep['repo'].startswith('MSYS2') else "mingw"
                release = repo.get_release("staging-" + repo_type)
                for asset in release.get_assets():
                    if fnmatch.fnmatch(asset.name, pattern):
                        to_add.append((repo_type, asset))
                        break
                else:
                    raise MissingDependencyError(f"asset for {pattern} not found")

            for repo_type, asset in to_add:
                add_to_repo(repo_root, repo_type, asset)

            # in case they are already installed we need to upgrade
            run_cmd(["pacman", "--noconfirm", "-Suy"])
            yield
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(["pacman", "--noconfirm", "-Suuy"])


def build_package(pkg, builddir):
    assert os.path.isabs(builddir)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    is_msys = pkg['repo'].startswith('MSYS2')

    with staging_dependencies(pkg, builddir), fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
        makepkg = 'makepkg' if is_msys else 'makepkg-mingw'

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
            ], cwd=pkg_dir, timeout=get_timeout())

            env = environ.copy()
            if not is_msys:
                env['MINGW_INSTALLS'] = 'mingw64'
            run_cmd([
                makepkg,
                '--noconfirm',
                '--noprogressbar',
                '--skippgpcheck',
                '--allsource'
            ], env=env, cwd=pkg_dir, timeout=get_timeout())
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
                if fnmatch.fnmatch(entry, '*.pkg.tar.*') or \
                        fnmatch.fnmatch(entry, '*.src.tar.*'):
                    path = os.path.join(pkg_dir, entry)
                    upload_asset("msys" if is_msys else "mingw", path)


def run_build(args):
    builddir = os.path.abspath(args.builddir)

    for pkg in get_packages_to_build()[2]:
        with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }..."):
            try:
                build_package(pkg, builddir)
            except MissingDependencyError as e:
                print("missing deps")
                print(e)
                continue
            except BuildTimeoutError:
                print("timeout")
                break
            except BuildError:
                print("failed")
                traceback.print_exc(file=sys.stdout)
                continue


def get_buildqueue():
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue")
    r.raise_for_status()
    dep_mapping = {}
    for pkg in r.json():
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)
        for name in pkg['packages']:
            dep_mapping[name] = pkg

    # extend depends with the version in the
    for pkg in pkgs:
        ver_depends = {}
        for dep in pkg['depends']:
            ver_depends[dep] = dep_mapping[dep]
        pkg['ext-depends'] = ver_depends

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
        elif (pkg_has_failed(pkg) or pkg['name'] in SKIP):
            skipped.append(pkg)
        else:
            todo.append(pkg)

    return done, skipped, todo


def show_build(args):
    done, skipped, todo = get_packages_to_build()

    def print_packages(title, pkgs):
        print()
        print(title)
        print(tabulate([(p["name"], p["version"]) for p in pkgs],
                       headers=["Package", "Version"]))

    print_packages("TODO:", todo)
    print_packages("SKIPPED:", skipped)
    print_packages("DONE:", done)


def show_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    for name in ["msys", "mingw"]:
        assets = repo.get_release('staging-' + name).get_assets()

        print(tabulate(
            [[
                asset.name,
                asset.size,
                asset.created_at,
                asset.updated_at,
            ] for asset in assets],
            headers=["name", "size", "created", "updated"]
        ))


def get_repo_subdir(type_, asset):
    entry = asset.name
    t = Path(type_)
    if type_ == "msys":
        if fnmatch.fnmatch(entry, '*.pkg.tar.*'):
            return t / "x86_64"
        elif fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        else:
            raise Exception("unknown file type")
    elif type_ == "mingw":
        if fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        elif entry.startswith("mingw-w64-x86_64-"):
            return t / "x86_64"
        elif entry.startswith("mingw-w64-i686-"):
            return t / "i686"
        else:
            raise Exception("unknown file type")


def fetch_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo('msys2/msys2-devtools')

    todo = []
    skipped = []
    for name in ["msys", "mingw"]:
        p = Path(args.targetdir)
        assets = repo.get_release('staging-' + name).get_assets()
        for asset in assets:
            asset_dir = p / get_repo_subdir(name, asset)
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
        asset, asset_path = item
        download_asset(asset, asset_path)
        return item

    with ThreadPoolExecutor(4) as executor:
        for i, item in enumerate(executor.map(fetch_item, todo)):
            print(f"[{i + 1}/{len(todo)}] {item[0].name}")

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

    sub = subparser.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser(
        "show-assets", help="Show all staging packages", allow_abbrev=False)
    sub.set_defaults(func=show_assets)

    sub = subparser.add_parser(
        "fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
    sub.set_defaults(func=fetch_assets)

    sub = subparser.add_parser("trigger", help="Trigger a GHA build", allow_abbrev=False)
    sub.set_defaults(func=trigger_gha_build)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    get_credentials()

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)
