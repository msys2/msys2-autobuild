#!/usr/bin/env python3

import sys
import os
import argparse
import glob
from os import environ
from github import Github
from github.GithubObject import GithubObject
from github.GithubException import GithubException, UnknownObjectException
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset
from github.Repository import Repository
from github.Workflow import Workflow
from pathlib import Path, PurePosixPath, PurePath
from subprocess import check_call
import subprocess
from sys import stdout
import fnmatch
import traceback
from tabulate import tabulate
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib3.util import Retry
import requests
import shlex
import time
import tempfile
import shutil
import json
import io
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Generator, Union, AnyStr, List, Any, Dict, Tuple, Set, Optional, Sequence


class Config:

    ALLOWED_UPLOADERS = [
        ("User", "elieux"),
        ("User", "Alexpux"),
        ("User", "lazka"),
        ("User", "jeremyd2019"),
        ("Bot", "github-actions[bot]"),
    ]
    """Users that are allowed to upload assets. This is checked at download time"""

    MINGW_ARCH_LIST = ["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64"]
    """Arches we try to build"""

    MINGW_SRC_ARCH = "mingw64"
    """The arch that is used to build the source package (any mingw one should work)"""

    REPO = "msys2/msys2-autobuild"
    """The path of this repo (used for accessing the assets)"""

    SOFT_JOB_TIMEOUT = 60 * 60 * 4
    """Runtime after which we shouldn't start a new build"""

    MANUAL_BUILD: List[str] = [
        'mingw-w64-firebird-git',
        'mingw-w64-qt5-static',
        'mingw-w64-arm-none-eabi-gcc',
    ]
    """Packages that take too long to build, and should be handled manually"""

    MANUAL_BUILD_TYPE: List[str] = ['clangarm64']
    """Build types that can't be built in CI"""

    IGNORE_RDEP_PACKAGES: List[str] = [
        "mingw-w64-mlpack",
        "mingw-w64-qemu",
        "mingw-w64-usbmuxd",
        "mingw-w64-arm-none-eabi-gcc",
        "mingw-w64-tolua",
        "mingw-w64-kirigami2-qt5",
        "mingw-w64-libgda",
    ]
    """XXX: These would in theory block rdeps, but no one fixed them, so we ignore them"""

    BUILD_TYPES_WIP: List[str] = ["clangarm64"]
    """XXX: These build types don't block other things, even if they fail/don't get built"""


_PathLike = Union[os.PathLike, AnyStr]

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

REQUESTS_TIMEOUT = (15, 30)


class PackageStatus(Enum):
    FINISHED = 'finished'
    FINISHED_BUT_BLOCKED = 'finished-but-blocked'
    FINISHED_BUT_INCOMPLETE = 'finished-but-incomplete'
    FAILED_TO_BUILD = 'failed-to-build'
    WAITING_FOR_BUILD = 'waiting-for-build'
    WAITING_FOR_DEPENDENCIES = 'waiting-for-dependencies'
    MANUAL_BUILD_REQUIRED = 'manual-build-required'
    UNKNOWN = 'unknown'

    def __str__(self) -> str:
        return self.value


def build_type_is_src(build_type: str) -> bool:
    return build_type in ["mingw-src", "msys-src"]


class Package(dict):

    def __repr__(self) -> str:
        return "Package(%r)" % self["name"]

    def __hash__(self) -> int:  # type: ignore
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def _get_build(self, build_type: str) -> Dict:
        return self["builds"].get(build_type, {})

    def get_status(self, build_type: str) -> PackageStatus:
        build = self._get_build(build_type)
        return build.get("status", PackageStatus.UNKNOWN)

    def get_status_details(self, build_type: str) -> Dict[str, Any]:
        build = self._get_build(build_type)
        return build.get("status_details", {})

    def set_status(self, build_type: str, status: PackageStatus,
                   description: Optional[str] = None,
                   urls: Optional[Dict[str, str]] = None) -> None:
        build = self["builds"].setdefault(build_type, {})
        build["status"] = status
        meta: Dict[str, Any] = {}
        if description:
            meta["desc"] = description
        if urls:
            meta["urls"] = urls
        build["status_details"] = meta

    def is_new(self, build_type: str) -> bool:
        build = self._get_build(build_type)
        return build.get("new", False)

    def get_build_patterns(self, build_type: str) -> List[str]:
        patterns = []
        if build_type_is_src(build_type):
            patterns.append(f"{self['name']}-{self['version']}.src.tar.[!s]*")
        elif build_type in (Config.MINGW_ARCH_LIST + ["msys"]):
            for item in self._get_build(build_type).get('packages', []):
                patterns.append(f"{item}-{self['version']}-*.pkg.tar.zst")
        else:
            assert 0
        return patterns

    def get_failed_names(self, build_type: str) -> List[str]:
        names = []
        if build_type_is_src(build_type):
            names.append(f"{self['name']}-{self['version']}.failed")
        elif build_type in (Config.MINGW_ARCH_LIST + ["msys"]):
            for item in self._get_build(build_type).get('packages', []):
                names.append(f"{item}-{self['version']}.failed")
        else:
            assert 0
        return names

    def get_build_types(self) -> List[str]:
        build_types = [
            t for t in self["builds"] if t in (Config.MINGW_ARCH_LIST + ["msys"])]
        if self["source"]:
            if any((k != 'msys') for k in build_types):
                build_types.append("mingw-src")
            if "msys" in build_types:
                build_types.append("msys-src")
        return build_types

    def _get_dep_build(self, build_type: str) -> Dict:
        if build_type == "mingw-src":
            build_type = Config.MINGW_SRC_ARCH
        elif build_type == "msys-src":
            build_type = "msys"
        return self._get_build(build_type)

    def get_depends(self, build_type: str) -> "Dict[str, Set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-depends', {})

    def get_rdepends(self, build_type: str) -> "Dict[str, Set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-rdepends', {})

    def get_repo_type(self) -> str:
        return "msys" if self['repo'].startswith('MSYS2') else "mingw"


def get_current_run_urls() -> Optional[Dict[str, str]]:
    # The only connection we have is the job name, so this depends
    # on unique job names in all workflows
    if "GITHUB_SHA" in os.environ and "GITHUB_RUN_NAME" in os.environ:
        sha = os.environ["GITHUB_SHA"]
        run_name = os.environ["GITHUB_RUN_NAME"]
        commit = get_repo().get_commit(sha)
        check_runs = commit.get_check_runs(
            check_name=run_name, status="in_progress", filter="latest")
        for run in check_runs:
            html = run.html_url + "?check_suite_focus=true"
            raw = commit.html_url + "/checks/" + str(run.id) + "/logs"
            return {"html": html, "raw": raw}
        else:
            raise Exception(f"No active job found for { run_name }")
    return None


def run_cmd(msys2_root: _PathLike, args: Sequence[_PathLike], **kwargs: Any) -> None:
    executable = os.path.join(msys2_root, 'usr', 'bin', 'bash.exe')
    env = clean_environ(kwargs.pop("env", os.environ.copy()))
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    env["MSYS2_PATH_TYPE"] = "minimal"

    def shlex_join(split_command: Sequence[str]) -> str:
        # shlex.join got added in 3.8 while we support 3.6
        return ' '.join(shlex.quote(arg) for arg in split_command)

    check_call([executable, '-lc'] + [shlex_join([str(a) for a in args])], env=env, **kwargs)


@contextmanager
def fresh_git_repo(url: str, path: _PathLike) -> Generator:
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])
    else:
        check_call(["git", "fetch", "origin"], cwd=path)
        check_call(["git", "reset", "--hard", "origin/master"], cwd=path)
    try:
        yield
    finally:
        assert os.path.exists(path)
        try:
            check_call(["git", "clean", "-xfdf"], cwd=path)
        except subprocess.CalledProcessError:
            # sometimes it fails right after the build has failed
            # not sure why
            pass
        check_call(["git", "reset", "--hard", "HEAD"], cwd=path)


@contextmanager
def gha_group(title: str) -> Generator:
    print(f'\n::group::{title}')
    stdout.flush()
    try:
        yield
    finally:
        print('::endgroup::')
        stdout.flush()


class BuildError(Exception):
    pass


def asset_is_complete(asset: GitReleaseAsset) -> bool:
    # assets can stay around in a weird incomplete state
    # in which case asset.state == "starter". GitHub shows
    # them with a red warning sign in the edit UI.
    return asset.state == "uploaded"


def fixup_datetime(dt: datetime) -> datetime:
    # pygithub returns datetime objects without a timezone
    # https://github.com/PyGithub/PyGithub/issues/1905
    assert dt.tzinfo is None
    return dt.replace(tzinfo=timezone.utc)


def get_asset_mtime_ns(asset: GitReleaseAsset) -> int:
    """Returns the mtime of an asset in nanoseconds"""

    updated_at = fixup_datetime(asset.updated_at)
    return int(updated_at.timestamp() * (1000 ** 3))


def download_asset(asset: GitReleaseAsset, target_path: str) -> None:
    assert asset_is_complete(asset)
    with requests.get(asset.browser_download_url, stream=True, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        fd, temppath = tempfile.mkstemp()
        try:
            os.chmod(temppath, 0o644)
            with os.fdopen(fd, "wb") as h:
                for chunk in r.iter_content(4096):
                    h.write(chunk)
            mtime_ns = get_asset_mtime_ns(asset)
            os.utime(temppath, ns=(mtime_ns, mtime_ns))
            shutil.move(temppath, target_path)
        finally:
            try:
                os.remove(temppath)
            except OSError:
                pass


def download_text_asset(asset: GitReleaseAsset) -> str:
    assert asset_is_complete(asset)
    with requests.get(asset.browser_download_url, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        return r.text


@contextmanager
def make_writable(obj: GithubObject) -> Generator:
    # XXX: This switches the read-only token with a potentially writable one
    old_requester = obj._requester  # type: ignore
    repo = get_repo(readonly=False)
    try:
        obj._requester = repo._requester  # type: ignore
        yield
    finally:
        obj._requester = old_requester  # type: ignore


def upload_asset(release: GitRelease, path: _PathLike, replace: bool = False,
                 text: bool = False, content: bytes = None) -> None:
    path = Path(path)
    basename = os.path.basename(str(path))
    asset_name = get_gh_asset_name(basename, text)
    asset_label = basename

    def can_try_upload_again() -> bool:
        for asset in get_release_assets(release, include_incomplete=True):
            if asset_name == asset.name:
                # We want to treat incomplete assets as if they weren't there
                # so replace them always
                if replace or not asset_is_complete(asset):
                    with make_writable(asset):
                        asset.delete_asset()
                    break
                else:
                    print(f"Skipping upload for {asset_name} as {asset_label}, already exists")
                    return False
        return True

    def upload() -> None:
        with make_writable(release):
            if content is None:
                release.upload_asset(str(path), label=asset_label, name=asset_name)
            else:
                with io.BytesIO(content) as fileobj:
                    release.upload_asset_from_memory(  # type: ignore
                        fileobj, len(content), label=asset_label, name=asset_name)

    try:
        upload()
    except (GithubException, requests.RequestException):
        if can_try_upload_again():
            upload()

    print(f"Uploaded {asset_name} as {asset_label}")


def get_python_path(msys2_root: _PathLike, msys2_path: _PathLike) -> Path:
    return Path(os.path.normpath(str(msys2_root) + str(msys2_path)))


def to_pure_posix_path(path: _PathLike) -> PurePath:
    return PurePosixPath("/" + str(path).replace(":", "", 1).replace("\\", "/"))


@contextmanager
def backup_pacman_conf(msys2_root: _PathLike) -> Generator:
    conf = get_python_path(msys2_root, "/etc/pacman.conf")
    backup = get_python_path(msys2_root, "/etc/pacman.conf.backup")
    shutil.copyfile(conf, backup)
    try:
        yield
    finally:
        os.replace(backup, conf)


@contextmanager
def staging_dependencies(
        build_type: str, pkg: Package, msys2_root: _PathLike,
        builddir: _PathLike) -> Generator:
    repo = get_repo()

    def add_to_repo(repo_root: str, repo_type: str, assets: List[GitReleaseAsset]) -> None:
        repo_dir = Path(repo_root) / repo_type
        os.makedirs(repo_dir, exist_ok=True)

        package_paths = []
        for asset in assets:
            print(f"Downloading {get_asset_filename(asset)}...")
            package_path = os.path.join(repo_dir, get_asset_filename(asset))
            download_asset(asset, package_path)
            package_paths.append(package_path)

        repo_name = f"autobuild-{repo_type}"
        repo_db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")

        conf = get_python_path(msys2_root, "/etc/pacman.conf")
        with open(conf, "r", encoding="utf-8") as h:
            text = h.read()
            uri = to_pure_posix_path(repo_dir).as_uri()
            if uri not in text:
                with open(conf, "w", encoding="utf-8") as h2:
                    h2.write(f"""[{repo_name}]
Server={uri}
SigLevel=Never
""")
                    h2.write(text)

        args: List[_PathLike] = ["repo-add", to_pure_posix_path(repo_db_path)]
        args += [to_pure_posix_path(p) for p in package_paths]
        run_cmd(msys2_root, args, cwd=repo_dir)

    def get_cached_assets(
            repo: Repository, release_name: str, *, _cache={}) -> List[GitReleaseAsset]:
        key = (repo.full_name, release_name)
        if key not in _cache:
            release = get_release(repo, release_name)
            _cache[key] = get_release_assets(release)
        return _cache[key]

    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with backup_pacman_conf(msys2_root):
            to_add: Dict[str, List[GitReleaseAsset]] = {}
            for dep_type, deps in pkg.get_depends(build_type).items():
                for dep in deps:
                    repo_type = dep.get_repo_type()
                    assets = get_cached_assets(repo, "staging-" + repo_type)
                    for pattern in dep.get_build_patterns(dep_type):
                        for asset in assets:
                            if fnmatch.fnmatch(get_asset_filename(asset), pattern):
                                to_add.setdefault(repo_type, []).append(asset)
                                break
                        else:
                            raise SystemExit(f"asset for {pattern} in {repo_type} not found")

            for repo_type, assets in to_add.items():
                add_to_repo(repo_root, repo_type, assets)

            # in case they are already installed we need to upgrade
            run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suy"])
            yield
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suuy"])


def build_package(build_type: str, pkg: Package, msys2_root: _PathLike, builddir: _PathLike) -> None:
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    to_upload: List[str] = []

    repo = get_repo()

    with staging_dependencies(build_type, pkg, msys2_root, builddir), \
            fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])

        try:
            # Fetch all keys mentioned in the PKGBUILD
            validpgpkeys = to_pure_posix_path(os.path.join(SCRIPT_DIR, 'fetch-validpgpkeys.sh'))
            run_cmd(msys2_root, ['bash', validpgpkeys], cwd=pkg_dir)

            if build_type == "mingw-src":
                env = environ.copy()
                env['MINGW_ARCH'] = Config.MINGW_SRC_ARCH
                run_cmd(msys2_root, [
                    'makepkg-mingw',
                    '--noconfirm',
                    '--noprogressbar',
                    '--allsource'
                ], env=env, cwd=pkg_dir)
            elif build_type == "msys-src":
                run_cmd(msys2_root, [
                    'makepkg',
                    '--noconfirm',
                    '--noprogressbar',
                    '--allsource'
                ], cwd=pkg_dir)
            elif build_type in Config.MINGW_ARCH_LIST:
                env = environ.copy()
                env['MINGW_ARCH'] = build_type
                run_cmd(msys2_root, [
                    'makepkg-mingw',
                    '--noconfirm',
                    '--noprogressbar',
                    '--nocheck',
                    '--syncdeps',
                    '--rmdeps',
                    '--cleanbuild'
                ], env=env, cwd=pkg_dir)
            elif build_type == "msys":
                run_cmd(msys2_root, [
                    'makepkg',
                    '--noconfirm',
                    '--noprogressbar',
                    '--nocheck',
                    '--syncdeps',
                    '--rmdeps',
                    '--cleanbuild'
                ], cwd=pkg_dir)
            else:
                assert 0

            entries = os.listdir(pkg_dir)
            for pattern in pkg.get_build_patterns(build_type):
                found = fnmatch.filter(entries, pattern)
                if not found:
                    raise BuildError(f"{pattern} not found, likely wrong version built")
                to_upload.extend([os.path.join(pkg_dir, e) for e in found])

        except (subprocess.CalledProcessError, BuildError) as e:
            wait_for_api_limit_reset()
            release = get_release(repo, "staging-failed")
            run_urls = get_current_run_urls()
            for entry in pkg.get_failed_names(build_type):
                failed_data = {}
                if run_urls is not None:
                    failed_data["urls"] = run_urls
                content = json.dumps(failed_data).encode()
                upload_asset(release, entry, text=True, content=content)

            raise BuildError(e)
        else:
            wait_for_api_limit_reset()
            release = repo.get_release("staging-" + pkg.get_repo_type())
            for path in to_upload:
                upload_asset(release, path)


def run_build(args: Any) -> None:
    builddir = os.path.abspath(args.builddir)
    msys2_root = os.path.abspath(args.msys2_root)
    if args.build_types is None:
        build_types = None
    else:
        build_types = [p.strip() for p in args.build_types.split(",")]

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

        if (time.monotonic() - start_time) >= Config.SOFT_JOB_TIMEOUT:
            print("timeout reached")
            break

        pkgs = get_buildqueue_with_status(full_details=True)
        update_status(pkgs)
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


def get_buildqueue() -> List[Package]:
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue2", timeout=REQUESTS_TIMEOUT)
    r.raise_for_status()

    for received in r.json():
        pkg = Package(received)
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)

    # extract the package mapping
    dep_mapping = {}
    for pkg in pkgs:
        for build in pkg['builds'].values():
            for name in build['packages']:
                dep_mapping[name] = pkg

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        for build in pkg['builds'].values():
            ver_depends: Dict[str, Set[Package]] = {}
            for repo, deps in build['depends'].items():
                for dep in deps:
                    ver_depends.setdefault(repo, set()).add(dep_mapping[dep])
            build['ext-depends'] = ver_depends

    # reverse dependencies
    for pkg in pkgs:
        for build in pkg['builds'].values():
            r_depends: Dict[str, Set[Package]] = {}
            for pkg2 in pkgs:
                for r_repo, build2 in pkg2['builds'].items():
                    for repo, deps in build2['ext-depends'].items():
                        if pkg in deps:
                            r_depends.setdefault(r_repo, set()).add(pkg2)
            build['ext-rdepends'] = r_depends

    return pkgs


def get_gh_asset_name(basename: _PathLike, text: bool = False) -> str:
    # GitHub will throw out charaters like '~' or '='. It also doesn't like
    # when there is no file extension and will try to add one
    return sha256(str(basename).encode("utf-8")).hexdigest() + (".bin" if not text else ".txt")


def get_asset_filename(asset: GitReleaseAsset) -> str:
    if not asset.label:
        return asset.name
    else:
        assert os.path.splitext(get_gh_asset_name(asset.label))[0] == \
            os.path.splitext(asset.name)[0]
        return asset.label


def get_release_assets(release: GitRelease, include_incomplete: bool = False) -> List[GitReleaseAsset]:
    assets = []
    for asset in release.get_assets():
        # skip in case not fully uploaded yet (or uploading failed)
        if not asset_is_complete(asset) and not include_incomplete:
            continue
        uploader = asset.uploader
        uploader_key = (uploader.type, uploader.login)
        # We allow uploads from some users and GHA
        if uploader_key not in Config.ALLOWED_UPLOADERS:
            raise SystemExit(
                f"ERROR: Asset '{get_asset_filename(asset)}' "
                f"uploaded by {uploader_key}'. Aborting.")
        assets.append(asset)
    return assets


def get_release(repo: Repository, name: str, create: bool = True) -> GitRelease:
    """Like Repository.get_release() but creates the referenced release if needed"""

    try:
        return repo.get_release(name)
    except UnknownObjectException:
        if not create:
            raise
        with make_writable(repo):
            return repo.create_git_release(name, name, name, prerelease=True)


def get_buildqueue_with_status(full_details: bool = False) -> List[Package]:
    repo = get_repo()
    assets = []
    for name in ["msys", "mingw"]:
        release = get_release(repo, 'staging-' + name)
        assets.extend(get_release_assets(release))
    release = get_release(repo, 'staging-failed')
    assets_failed = get_release_assets(release)

    failed_urls = {}
    if full_details:
        # This might take a while, so only in full mode
        with ThreadPoolExecutor(8) as executor:
            for i, (asset, content) in enumerate(
                    zip(assets_failed, executor.map(download_text_asset, assets_failed))):
                result = json.loads(content)
                if result["urls"]:
                    failed_urls[get_asset_filename(asset)] = result["urls"]

    def pkg_is_done(build_type: str, pkg: Package) -> bool:
        done_names = [get_asset_filename(a) for a in assets]
        for pattern in pkg.get_build_patterns(build_type):
            if not fnmatch.filter(done_names, pattern):
                return False
        return True

    def get_failed_urls(build_type: str, pkg: Package) -> Optional[Dict[str, str]]:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        for name in pkg.get_failed_names(build_type):
            if name in failed_names:
                return failed_urls.get(name)
        return None

    def pkg_has_failed(build_type: str, pkg: Package) -> bool:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        for name in pkg.get_failed_names(build_type):
            if name in failed_names:
                return True
        return False

    def pkg_is_manual(build_type: str, pkg: Package) -> bool:
        if build_type_is_src(build_type):
            return False
        return pkg['name'] in Config.MANUAL_BUILD or build_type in Config.MANUAL_BUILD_TYPE

    pkgs = get_buildqueue()

    # basic state
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg_is_done(build_type, pkg):
                pkg.set_status(build_type, PackageStatus.FINISHED)
            elif pkg_has_failed(build_type, pkg):
                urls = get_failed_urls(build_type, pkg)
                pkg.set_status(build_type, PackageStatus.FAILED_TO_BUILD, urls=urls)
            elif pkg_is_manual(build_type, pkg):
                pkg.set_status(build_type, PackageStatus.MANUAL_BUILD_REQUIRED)
            else:
                pkg.set_status(build_type, PackageStatus.WAITING_FOR_BUILD)

    # wait for dependencies to be finished before starting a build
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            if status == PackageStatus.WAITING_FOR_BUILD:
                missing_deps = set()
                for dep_type, deps in pkg.get_depends(build_type).items():
                    for dep in deps:
                        dep_status = dep.get_status(dep_type)
                        if dep_status != PackageStatus.FINISHED:
                            missing_deps.add(dep)
                if missing_deps:
                    desc = f"Waiting for: {', '.join(sorted(d['name'] for d in missing_deps))}"
                    pkg.set_status(build_type, PackageStatus.WAITING_FOR_DEPENDENCIES, desc)

    # Block packages where not all deps/rdeps/related are finished
    changed = True
    while changed:
        changed = False
        for pkg in pkgs:
            for build_type in pkg.get_build_types():
                status = pkg.get_status(build_type)
                if status == PackageStatus.FINISHED:
                    # src builds are independent
                    if build_type_is_src(build_type):
                        continue

                    missing_deps = set()
                    for dep_type, deps in pkg.get_depends(build_type).items():
                        for dep in deps:
                            dep_status = dep.get_status(dep_type)
                            if dep_status != PackageStatus.FINISHED:
                                missing_deps.add(dep)

                    missing_rdeps = set()
                    for dep_type, deps in pkg.get_rdepends(build_type).items():
                        for dep in deps:
                            if dep["name"] in Config.IGNORE_RDEP_PACKAGES or \
                                    (build_type != dep_type and dep_type in Config.BUILD_TYPES_WIP):
                                continue
                            dep_status = dep.get_status(dep_type)
                            dep_new = dep.is_new(dep_type)
                            # if the rdep isn't in the repo we can't break it by uploading
                            if dep_status != PackageStatus.FINISHED and not dep_new:
                                missing_rdeps.add(dep)

                    descs = []
                    if missing_deps:
                        desc = (f"Waiting on dependencies: "
                                f"{ ', '.join(sorted(p['name'] for p in missing_deps)) }")
                        descs.append(desc)
                    if missing_rdeps:
                        desc = (f"Waiting on reverse dependencies: "
                                f"{ ', '.join(sorted(p['name'] for p in missing_rdeps)) }")
                        descs.append(desc)

                    if descs:
                        changed = True
                        pkg.set_status(
                            build_type, PackageStatus.FINISHED_BUT_BLOCKED, ". ".join(descs))

        # Block packages where not every build type is finished
        for pkg in pkgs:
            unfinished = []
            blocked = []
            finished = []
            for build_type in pkg.get_build_types():
                status = pkg.get_status(build_type)
                if status != PackageStatus.FINISHED:
                    if status == PackageStatus.FINISHED_BUT_BLOCKED:
                        blocked.append(build_type)
                    # if the package isn't in the repo better not block on it
                    elif not pkg.is_new(build_type):
                        if build_type not in Config.BUILD_TYPES_WIP:
                            unfinished.append(build_type)
                else:
                    finished.append(build_type)

            # We track source packages by assuming they are in the repo if there is
            # at least one binary package in the repo. Uploading lone source
            # packages will not change anything, so block them.
            if not blocked and not unfinished and finished and \
                    all(build_type_is_src(bt) for bt in finished):
                unfinished.append("any")

            if unfinished:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED):
                        desc = f"Missing related builds: {', '.join(sorted(unfinished))}"
                        changed = True
                        pkg.set_status(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE, desc)
            elif blocked:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status == PackageStatus.FINISHED:
                        desc = f"Related build blocked: {', '.join(sorted(blocked))}"
                        changed = True
                        pkg.set_status(build_type, PackageStatus.FINISHED_BUT_BLOCKED, desc)

    return pkgs


def get_package_to_build(
        pkgs: List[Package], build_types: Optional[List[str]],
        build_from: str) -> Optional[Tuple[Package, str]]:

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


def get_workflow() -> Workflow:
    workflow_name = os.environ.get("GITHUB_WORKFLOW", None)
    if workflow_name is None:
        raise Exception("GITHUB_WORKFLOW not set")
    repo = get_repo()
    for workflow in repo.get_workflows():
        if workflow.name == workflow_name:
            return workflow
    else:
        raise Exception("workflow not found:", workflow_name)


def get_job_meta() -> List[Dict[str, Any]]:
    job_meta: List[Dict[str, Any]] = [
        {
            "build-types": ["mingw64"],
            "matrix": {
                "packages": "base-devel mingw-w64-x86_64-toolchain git",
                "build-args": "--build-types mingw64",
                "name": "mingw64",
                "runner": "windows-latest"
            }
        }, {
            "build-types": ["mingw32"],
            "matrix": {
                "packages": "base-devel mingw-w64-i686-toolchain git",
                "build-args": "--build-types mingw32",
                "name": "mingw32",
                "runner": "windows-latest"
            }
        }, {
            "build-types": ["ucrt64"],
            "matrix": {
                "packages": "base-devel mingw-w64-ucrt-x86_64-toolchain git",
                "build-args": "--build-types ucrt64",
                "name": "ucrt64",
                "runner": "windows-latest"
            }
        }, {
            "build-types": ["clang64"],
            "matrix": {
                "packages": "base-devel mingw-w64-clang-x86_64-toolchain git",
                "build-args": "--build-types clang64",
                "name": "clang64",
                "runner": "windows-latest"
            }
        }, {
            "build-types": ["clang32"],
            "matrix": {
                "packages": "base-devel git",
                "build-args": "--build-types clang32",
                "name": "clang32",
                "runner": "windows-latest"
            }
        }, {
            "build-types": ["clangarm64"],
            "matrix": {
                "packages": "base-devel git",
                "build-args": "--build-types clangarm64",
                "name": "clangarm64",
                "runner": ["Windows", "ARM64"]
            }
        }, {
            "build-types": ["msys", "msys-src"],
            "matrix": {
                "packages": "base-devel msys2-devel VCS",
                "build-args": "--build-types msys,msys-src",
                "name": "msys",
                "runner": "windows-latest"
            }
        }
    ]

    # The job matching MINGW_SRC_ARCH should also build mingw-src
    for meta in job_meta:
        if Config.MINGW_SRC_ARCH in meta["build-types"]:
            meta["build-types"].append("mingw-src")
            meta["matrix"]["build-args"] = meta["matrix"]["build-args"] + ",mingw-src"
            meta["matrix"]["packages"] = meta["matrix"]["packages"] + " VCS"
            break
    else:
        raise Exception("Didn't find arch for building mingw-src")

    return job_meta


def write_build_plan(args: Any) -> None:
    target_file = args.target_file

    current_id = None
    if "GITHUB_RUN_ID" in os.environ:
        current_id = int(os.environ["GITHUB_RUN_ID"])

    def write_out(result: List[Dict[str, str]]) -> None:
        with open(target_file, "wb") as h:
            h.write(json.dumps(result).encode())

    workflow = get_workflow()
    runs = list(workflow.get_runs(status="in_progress"))
    runs += list(workflow.get_runs(status="queued"))
    for run in runs:
        if current_id is not None and current_id == run.id:
            # Ignore this run itself
            continue
        print(f"Another workflow is currently running or has something queued: {run.html_url}")
        write_out([])
        return

    wait_for_api_limit_reset()

    pkgs = get_buildqueue_with_status(full_details=True)
    update_status(pkgs)

    queued_build_types: Dict[str, int] = {}
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                queued_build_types[build_type] = queued_build_types.get(build_type, 0) + 1

    if not queued_build_types:
        write_out([])
        return

    jobs = []
    for job_info in get_job_meta():
        matching_build_types = set(queued_build_types) & set(job_info["build-types"])
        if matching_build_types:
            build_count = sum(queued_build_types[bt] for bt in matching_build_types)
            jobs.append(job_info["matrix"])
            # XXX: If there is more than three builds we start two jobs with the second
            # one having a reversed build order
            if build_count > 3:
                matrix = dict(job_info["matrix"])
                matrix["build-args"] = matrix["build-args"] + " --build-from end"
                matrix["name"] = matrix["name"] + "-2"
                jobs.append(matrix)
            if build_count > 9:
                matrix = dict(job_info["matrix"])
                matrix["build-args"] = matrix["build-args"] + " --build-from middle"
                matrix["name"] = matrix["name"] + "-3"
                jobs.append(matrix)

    write_out(jobs)


def queue_website_update() -> None:
    r = requests.post('https://packages.msys2.org/api/trigger_update', timeout=REQUESTS_TIMEOUT)
    r.raise_for_status()


def update_status(pkgs: List[Package]) -> None:
    repo = get_repo()
    release = get_release(repo, "status")

    results = {}
    for pkg in pkgs:
        pkg_result = {}
        for build_type in pkg.get_build_types():
            details = pkg.get_status_details(build_type)
            details["status"] = pkg.get_status(build_type).value
            details["version"] = pkg["version"]
            pkg_result[build_type] = details
        results[pkg["name"]] = pkg_result

    content = json.dumps(results, indent=2).encode()

    # If multiple jobs update this at the same time things can fail
    try:
        asset_name = "status.json"
        for asset in release.get_assets():
            if asset.name == asset_name:
                with make_writable(asset):
                    asset.delete_asset()
                break
        with io.BytesIO(content) as fileobj:
            with make_writable(release):
                new_asset = release.upload_asset_from_memory(  # type: ignore
                    fileobj, len(content), asset_name)
    except (GithubException, requests.RequestException) as e:
        print(e)
        return

    print(f"Uploaded status file for {len(results)} packages: {new_asset.browser_download_url}")

    queue_website_update()


def run_update_status(args: Any) -> None:
    update_status(get_buildqueue_with_status(full_details=True))


def show_build(args: Any) -> None:
    todo = []
    waiting = []
    done = []
    failed = []

    for pkg in get_buildqueue_with_status(full_details=True):
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            details = pkg.get_status_details(build_type)
            if status == PackageStatus.WAITING_FOR_BUILD:
                todo.append((pkg, build_type, status, details))
            elif status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED,
                            PackageStatus.FINISHED_BUT_INCOMPLETE):
                done.append((pkg, build_type, status, details))
            elif status in (PackageStatus.WAITING_FOR_DEPENDENCIES,
                            PackageStatus.MANUAL_BUILD_REQUIRED):
                waiting.append((pkg, build_type, status, details))
            else:
                failed.append((pkg, build_type, status, details))

    def show_table(name: str, items: List) -> None:
        with gha_group(f"{name} ({len(items)})"):
            print(tabulate([(p["name"], bt, p["version"], str(s), d) for (p, bt, s, d) in items],
                           headers=["Package", "Build", "Version", "Status", "Details"]))

    show_table("TODO", todo)
    show_table("WAITING", waiting)
    show_table("FAILED", failed)
    show_table("DONE", done)


def get_repo_subdir(type_: str, asset: GitReleaseAsset) -> Path:
    entry = get_asset_filename(asset)
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
        elif entry.startswith("mingw-w64-ucrt-x86_64-"):
            return t / "ucrt64"
        elif entry.startswith("mingw-w64-clang-x86_64-"):
            return t / "clang64"
        elif entry.startswith("mingw-w64-clang-i686-"):
            return t / "clang32"
        elif entry.startswith("mingw-w64-clang-aarch64-"):
            return t / "clangarm64"
        else:
            raise Exception("unknown file type")
    else:
        raise Exception("unknown type")


def upload_assets(args: Any) -> None:
    repo = get_repo()
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

    patterns = []
    for pkg in pkgs:
        repo_type = pkg.get_repo_type()
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)

            # ignore finished packages
            if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED,
                          PackageStatus.FINISHED_BUT_INCOMPLETE):
                continue

            patterns.extend(pkg.get_build_patterns(build_type))

    print(f"Looking for the following files in {src_dir}:")
    for pattern in patterns:
        print("  ", pattern)

    matches = []
    for pattern in patterns:
        for match in glob.glob(os.path.join(src_dir, pattern)):
            matches.append(match)
    print(f"Found {len(matches)} files..")

    release = get_release(repo, 'staging-' + repo_type)
    for match in matches:
        print(f"Uploading {match}")
        if not args.dry_run:
            upload_asset(release, match)
    print("Done")


def fetch_assets(args: Any) -> None:
    repo = get_repo()
    target_dir = os.path.abspath(args.targetdir)
    fetch_all = args.fetch_all

    all_patterns: Dict[str, List[str]] = {}
    all_blocked = []
    for pkg in get_buildqueue_with_status():
        repo_type = pkg.get_repo_type()
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            pkg_patterns = pkg.get_build_patterns(build_type)
            if status == PackageStatus.FINISHED:
                all_patterns.setdefault(repo_type, []).extend(pkg_patterns)
            elif status in [PackageStatus.FINISHED_BUT_BLOCKED,
                            PackageStatus.FINISHED_BUT_INCOMPLETE]:
                if fetch_all:
                    all_patterns.setdefault(repo_type, []).extend(pkg_patterns)
                else:
                    all_blocked.append(
                        (pkg["name"], build_type, pkg.get_status_details(build_type)))

    all_assets = {}
    assets_to_download: Dict[str, List[GitReleaseAsset]] = {}
    for repo_type, patterns in all_patterns.items():
        if repo_type not in all_assets:
            release = get_release(repo, 'staging-' + repo_type)
            all_assets[repo_type] = get_release_assets(release)
        assets = all_assets[repo_type]

        assets_mapping: Dict[str, List[GitReleaseAsset]] = {}
        for asset in assets:
            assets_mapping.setdefault(get_asset_filename(asset), []).append(asset)

        for pattern in patterns:
            matches = fnmatch.filter(assets_mapping.keys(), pattern)
            if matches:
                found = assets_mapping[matches[0]]
                assets_to_download.setdefault(repo_type, []).extend(found)

    to_fetch = {}
    for repo_type, assets in assets_to_download.items():
        for asset in assets:
            asset_dir = Path(target_dir) / get_repo_subdir(repo_type, asset)
            asset_path = asset_dir / get_asset_filename(asset)
            to_fetch[str(asset_path)] = asset

    def file_is_uptodate(path, asset):
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
    print("Pass --fetch-all to also fetch blocked packages.")
    print("Pass --delete to clear the target directory")

    def fetch_item(item):
        asset_path, asset = item
        if not args.pretend:
            download_asset(asset, asset_path)
        return item

    with ThreadPoolExecutor(8) as executor:
        for i, item in enumerate(executor.map(fetch_item, todo.items())):
            print(f"[{i + 1}/{len(todo)}] {get_asset_filename(item[1])}")

    print("done")


def get_assets_to_delete(repo: Repository) -> List[GitReleaseAsset]:
    print("Fetching packages to build...")
    patterns = []
    for pkg in get_buildqueue():
        for build_type in pkg.get_build_types():
            patterns.extend(pkg.get_failed_names(build_type))
            patterns.extend(pkg.get_build_patterns(build_type))

    print("Fetching assets...")
    assets: Dict[str, List[GitReleaseAsset]] = {}
    for release_name in ['staging-msys', 'staging-mingw', 'staging-failed']:
        release = get_release(repo, release_name)
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
    repo = get_repo()
    assets = get_assets_to_delete(repo)

    for asset in assets:
        print(f"Deleting {get_asset_filename(asset)}...")
        if not args.dry_run:
            with make_writable(asset):
                asset.delete_asset()

    if not assets:
        print("Nothing to delete")


def clear_failed_state(args: Any) -> None:
    build_type = args.build_type
    repo = get_repo()
    release = get_release(repo, 'staging-failed')
    assets_failed = get_release_assets(release)
    failed_map = dict((get_asset_filename(a), a) for a in assets_failed)

    for pkg in get_buildqueue_with_status():
        for name in pkg.get_failed_names(build_type):
            if name in failed_map:
                asset = failed_map[name]
                print(f"Deleting {get_asset_filename(asset)}...")
                if not args.dry_run:
                    with make_writable(asset):
                        asset.delete_asset()


def get_credentials(readonly: bool = True) -> Dict[str, Any]:
    if readonly and environ.get("GITHUB_TOKEN_READONLY", ""):
        return {'login_or_token': environ["GITHUB_TOKEN_READONLY"]}
    elif "GITHUB_TOKEN" in environ:
        return {'login_or_token': environ["GITHUB_TOKEN"]}
    else:
        if readonly:
            print("[Warning] 'GITHUB_TOKEN' or 'GITHUB_TOKEN_READONLY' env vars "
                  "not set which might lead to API rate limiting", file=sys.stderr)
            return {}
        else:
            raise Exception("'GITHUB_TOKEN' env var not set")


def get_github(readonly: bool = True) -> Github:
    kwargs = get_credentials(readonly=readonly)
    has_creds = bool(kwargs)
    # 100 is the maximum allowed
    kwargs['per_page'] = 100
    kwargs['retry'] = Retry(total=3, backoff_factor=1)
    gh = Github(**kwargs)
    if not has_creds and readonly:
        print(f"[Warning] Rate limit status: {gh.get_rate_limit().core}", file=sys.stderr)
    return gh


def get_repo(readonly: bool = True) -> Repository:
    gh = get_github(readonly=readonly)
    return gh.get_repo(Config.REPO, lazy=True)


def wait_for_api_limit_reset(
        min_remaining: int = 50, min_remaining_readonly: int = 250, min_sleep: float = 60,
        max_sleep: float = 300) -> None:

    for readonly in [True, False]:
        gh = get_github(readonly=readonly)
        while True:
            core = gh.get_rate_limit().core
            reset = fixup_datetime(core.reset)
            now = datetime.now(timezone.utc)
            diff = (reset - now).total_seconds()
            print(f"{core.remaining} API calls left (readonly={readonly}), "
                  f"{diff} seconds until the next reset")
            if core.remaining > (min_remaining_readonly if readonly else min_remaining):
                break
            wait = diff
            if wait < min_sleep:
                wait = min_sleep
            elif wait > max_sleep:
                wait = max_sleep
            print(f"Too few API calls left, waiting for {wait} seconds", flush=True)
            time.sleep(wait)


def clean_environ(environ: Dict[str, str]) -> Dict[str, str]:
    """Returns an environment without any CI related variables.

    This is to avoid leaking secrets to package build scripts we call.
    While in theory we trust them this can't hurt.
    """

    new_env = environ.copy()
    for key in list(new_env):
        if key.startswith(("GITHUB_", "RUNNER_")):
            del new_env[key]
    return new_env


def main(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.set_defaults(func=lambda *x: parser.print_help())
    parser.add_argument(
           "-R", "--repo", action="store",
           help=f"msys2-autobuild repository to target (default '{Config.REPO}')", default=Config.REPO)
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("-t", "--build-types", action="store")
    sub.add_argument(
        "--build-from", action="store", default="start", help="Start building from start|end|middle")
    sub.add_argument("msys2_root", help="The MSYS2 install used for building. e.g. C:\\msys64")
    sub.add_argument(
        "builddir",
        help="A directory used for saving temporary build results and the git repos")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser(
        "write-build-plan", help="Write a GHA build matrix setup", allow_abbrev=False)
    sub.add_argument("target_file")
    sub.set_defaults(func=write_build_plan)

    sub = subparser.add_parser(
        "update-status", help="Update the status file", allow_abbrev=False)
    sub.set_defaults(func=run_update_status)

    sub = subparser.add_parser(
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
    sub.set_defaults(func=fetch_assets)

    sub = subparser.add_parser(
        "upload-assets", help="Upload packages", allow_abbrev=False)
    sub.add_argument("path", help="Directory to look for packages in")
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be uploaded")
    sub.add_argument("-p", "--package", action="store", help=(
        "Only upload files belonging to a particualr package (pkgbase)"))
    sub.set_defaults(func=upload_assets)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    sub = subparser.add_parser(
        "clear-failed", help="Clear the failed state for a build type", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.add_argument("build_type")
    sub.set_defaults(func=clear_failed_state)

    args = parser.parse_args(argv[1:])
    Config.REPO = args.repo
    args.func(args)


if __name__ == "__main__":
    main(sys.argv)
