#!/usr/bin/env python3

import sys
import os
import argparse
import glob
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
import fnmatch
import traceback
from tabulate import tabulate
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib3.util import Retry
import requests
from requests.adapters import HTTPAdapter
import shlex
import time
import tempfile
import shutil
import json
import io
from functools import lru_cache
from datetime import datetime, timezone, timedelta
from enum import Enum
from hashlib import sha256
from typing import Generator, Union, AnyStr, List, Any, Dict, Tuple, Set, Optional, Sequence, \
    Literal, cast, TypeVar


ArchType = Literal["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64", "msys"]
SourceType = Literal["mingw-src", "msys-src"]
BuildType = Union[ArchType, SourceType]


class Config:

    ALLOWED_UPLOADERS = [
        ("User", "elieux"),
        ("User", "Alexpux"),
        ("User", "lazka"),
        ("User", "jeremyd2019"),
        ("Bot", "github-actions[bot]"),
    ]
    """Users that are allowed to upload assets. This is checked at download time"""

    MINGW_ARCH_LIST: List[ArchType] = ["mingw32", "mingw64", "ucrt64", "clang64", "clang32", "clangarm64"]
    """Arches we try to build"""

    MINGW_SRC_ARCH: ArchType = "ucrt64"
    """The arch that is used to build the source package (any mingw one should work)"""

    MAIN_REPO = "msys2/msys2-autobuild"
    """The path of this repo (used for accessing the assets)"""

    ASSETS_REPO: Dict[BuildType, str] = {
        "msys-src": "msys2/msys2-autobuild",
        "msys": "msys2/msys2-autobuild",
        "mingw-src": "msys2/msys2-autobuild",
        "mingw32": "msys2/msys2-autobuild",
        "mingw64": "msys2/msys2-autobuild",
        "ucrt64": "msys2/msys2-autobuild",
        "clang64": "msys2/msys2-autobuild",
        "clang32": "msys2/msys2-autobuild",
        "clangarm64": "msys2-arm/msys2-autobuild",
    }
    """Fetch certain build types from other repos if available"""

    SOFT_JOB_TIMEOUT = 60 * 60 * 3
    """Runtime after which we shouldn't start a new build"""

    MAXIMUM_JOB_COUNT = 15
    """Maximum number of jobs to spawn"""

    MANUAL_BUILD: List[Tuple[str, List[BuildType]]] = [
        ('mingw-w64-firebird-git', []),
        ('mingw-w64-qt5-static', ['mingw32', 'mingw64', 'ucrt64', 'clang32', 'clang64']),
        ('mingw-w64-qt6-static', ['mingw32', 'mingw64', 'ucrt64', 'clang32', 'clang64']),
        ('mingw-w64-arm-none-eabi-gcc', []),
    ]
    """Packages that take too long to build, or can't be build and should be handled manually"""

    IGNORE_RDEP_PACKAGES: List[str] = [
        'mingw-w64-qt5-static',
        'mingw-w64-qt6-static',
        'mingw-w64-zig',
    ]
    """XXX: These would in theory block rdeps, but no one fixed them, so we ignore them"""

    OPTIONAL_DEPS: Dict[str, List[str]] = {
        "mingw-w64-headers-git": ["mingw-w64-winpthreads-git", "mingw-w64-tools-git"],
        "mingw-w64-crt-git": ["mingw-w64-winpthreads-git"],
        "mingw-w64-clang": ["mingw-w64-libc++"],
    }
    """XXX: In case of cycles we mark these deps as optional"""


_PathLike = Union[os.PathLike, AnyStr]

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

REQUESTS_TIMEOUT = (15, 30)
REQUESTS_RETRY = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502])


@lru_cache(maxsize=None)
def get_requests_session(nocache=False) -> requests.Session:
    adapter = HTTPAdapter(max_retries=REQUESTS_RETRY)
    if nocache:
        with requests_cache_disabled():
            http = requests.Session()
    else:
        http = requests.Session()
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    return http


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


def build_type_is_src(build_type: BuildType) -> bool:
    return build_type in ["mingw-src", "msys-src"]


class Package(dict):

    def __repr__(self) -> str:
        return "Package(%r)" % self["name"]

    def __hash__(self) -> int:  # type: ignore
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def _get_build(self, build_type: BuildType) -> Dict:
        return self["builds"].get(build_type, {})

    def get_status(self, build_type: BuildType) -> PackageStatus:
        build = self._get_build(build_type)
        return build.get("status", PackageStatus.UNKNOWN)

    def get_status_details(self, build_type: BuildType) -> Dict[str, Any]:
        build = self._get_build(build_type)
        return dict(build.get("status_details", {}))

    def set_status(self, build_type: BuildType, status: PackageStatus,
                   description: Optional[str] = None,
                   urls: Optional[Dict[str, str]] = None) -> None:
        build = self["builds"].setdefault(build_type, {})
        build["status"] = status
        meta: Dict[str, Any] = {}
        meta["desc"] = description
        if urls is None:
            urls = {}
        meta["urls"] = urls
        build["status_details"] = meta

    def set_blocked(
            self, build_type: BuildType, status: PackageStatus,
            dep: "Package", dep_type: BuildType):
        dep_details = dep.get_status_details(dep_type)
        dep_blocked = dep_details.get("blocked", {})
        details = self.get_status_details(build_type)
        blocked = details.get("blocked", {})
        if dep_blocked:
            blocked = dict(dep_blocked)
        else:
            blocked.setdefault(dep, set()).add(dep_type)
        descs = []
        for pkg, types in blocked.items():
            descs.append("%s (%s)" % (pkg["name"], "/".join(types)))
        self.set_status(build_type, status, "Blocked by: " + ", ".join(descs))
        build = self._get_build(build_type)
        build.setdefault("status_details", {})["blocked"] = blocked

    def is_new(self, build_type: BuildType) -> bool:
        build = self._get_build(build_type)
        return build.get("new", False)

    def get_build_patterns(self, build_type: BuildType) -> List[str]:
        patterns = []
        if build_type_is_src(build_type):
            patterns.append(f"{self['name']}-{self['version']}.src.tar.[!s]*")
        elif build_type in (Config.MINGW_ARCH_LIST + ["msys"]):
            for item in self._get_build(build_type).get('packages', []):
                patterns.append(f"{item}-{self['version']}-*.pkg.tar.zst")
        else:
            assert 0
        return patterns

    def get_failed_name(self, build_type: BuildType) -> str:
        return f"{build_type}-{self['name']}-{self['version']}.failed"

    def get_build_types(self) -> List[BuildType]:
        build_types = [
            t for t in self["builds"] if t in (Config.MINGW_ARCH_LIST + ["msys"])]
        if self["source"]:
            if any((k != 'msys') for k in build_types):
                build_types.append("mingw-src")
            if "msys" in build_types:
                build_types.append("msys-src")
        return build_types

    def _get_dep_build(self, build_type: BuildType) -> Dict:
        if build_type == "mingw-src":
            build_type = Config.MINGW_SRC_ARCH
        elif build_type == "msys-src":
            build_type = "msys"
        return self._get_build(build_type)

    def is_optional_dep(self, dep: "Package", dep_type: BuildType):
        # Some deps are manually marked as optional to break cycles.
        # This requires them to be in the main repo though, otherwise the cycle has to
        # be fixed manually.
        return dep["name"] in Config.OPTIONAL_DEPS.get(self["name"], []) and not dep.is_new(dep_type)

    def get_depends(self, build_type: BuildType) -> "Dict[ArchType, Set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-depends', {})

    def get_rdepends(self, build_type: BuildType) -> "Dict[ArchType, Set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-rdepends', {})


def get_current_run_urls() -> Optional[Dict[str, str]]:
    # The only connection we have is the job name, so this depends
    # on unique job names in all workflows
    if "GITHUB_SHA" in os.environ and "GITHUB_RUN_NAME" in os.environ:
        sha = os.environ["GITHUB_SHA"]
        run_name = os.environ["GITHUB_RUN_NAME"]
        commit = get_main_repo().get_commit(sha)
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
    try:
        yield
    finally:
        print('::endgroup::')


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
    session = get_requests_session(nocache=True)
    with session.get(asset.browser_download_url, stream=True, timeout=REQUESTS_TIMEOUT) as r:
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
    session = get_requests_session()
    with session.get(asset.browser_download_url, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        return r.text


@contextmanager
def make_writable(obj: GithubObject) -> Generator:
    # XXX: This switches the read-only token with a potentially writable one
    old_requester = obj._requester  # type: ignore
    repo = get_main_repo(readonly=False)
    try:
        obj._requester = repo._requester  # type: ignore
        yield
    finally:
        obj._requester = old_requester  # type: ignore


def upload_asset(release: GitRelease, path: _PathLike, replace: bool = False,
                 text: bool = False, content: Optional[bytes] = None) -> None:
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
def temp_pacman_conf(msys2_root: _PathLike) -> Generator[Path, None, None]:
    """Gives a unix path to a temporary copy of pacman.conf"""

    fd, filename = tempfile.mkstemp("pacman.conf")
    os.close(fd)
    try:
        conf = get_python_path(msys2_root, "/etc/pacman.conf")
        with open(conf, "rb") as src:
            with open(filename, "wb") as dest:
                shutil.copyfileobj(src, dest)

        yield Path(filename)
    finally:
        try:
            os.unlink(filename)
        except OSError:
            pass


@contextmanager
def temp_pacman_script(pacman_config: _PathLike) -> Generator[_PathLike, None, None]:
    """Gives a temporary pacman script which uses the passed in pacman config
    without having to pass --config to it. Required because makepkg doesn't allow
    setting the pacman conf path, but it allows setting the pacman executable path
    via the 'PACMAN' env var.
    """

    fd, filename = tempfile.mkstemp("pacman")
    os.close(fd)

    try:
        with open(filename, "w", encoding="utf-8") as h:
            cli = shlex.join(['/usr/bin/pacman', '--config', str(to_pure_posix_path(pacman_config))])
            h.write(f"""\
#!/bin/bash
set -e
exec {cli} "$@"
""")
        yield filename
    finally:
        try:
            os.unlink(filename)
        except OSError:
            pass


@contextmanager
def staging_dependencies(
        build_type: BuildType, pkg: Package, msys2_root: _PathLike,
        builddir: _PathLike) -> Generator[_PathLike, None, None]:

    def add_to_repo(repo_root: _PathLike, pacman_config: _PathLike, repo_name: str,
                    assets: List[GitReleaseAsset]) -> None:
        repo_dir = Path(repo_root) / repo_name
        os.makedirs(repo_dir, exist_ok=True)

        todo = []
        for asset in assets:
            asset_path = os.path.join(repo_dir, get_asset_filename(asset))
            todo.append((asset_path, asset))

        def fetch_item(item):
            asset_path, asset = item
            download_asset(asset, asset_path)
            return item

        package_paths = []
        with ThreadPoolExecutor(8) as executor:
            for i, item in enumerate(executor.map(fetch_item, todo)):
                asset_path, asset = item
                print(f"[{i + 1}/{len(todo)}] {get_asset_filename(asset)}")
                package_paths.append(asset_path)

        repo_name = f"autobuild-{repo_name}"
        repo_db_path = os.path.join(repo_dir, f"{repo_name}.db.tar.gz")

        with open(pacman_config, "r", encoding="utf-8") as h:
            text = h.read()
            uri = to_pure_posix_path(repo_dir).as_uri()
            if uri not in text:
                with open(pacman_config, "w", encoding="utf-8") as h2:
                    h2.write(f"""[{repo_name}]
Server={uri}
SigLevel=Never
""")
                    h2.write(text)

        # repo-add 15 packages at a time so we don't hit the size limit for CLI arguments
        ChunkItem = TypeVar("ChunkItem")

        def chunks(lst: List[ChunkItem], n: int) -> Generator[List[ChunkItem], None, None]:
            for i in range(0, len(lst), n):
                yield lst[i:i + n]

        base_args: List[_PathLike] = ["repo-add", to_pure_posix_path(repo_db_path)]
        posix_paths: List[_PathLike] = [to_pure_posix_path(p) for p in package_paths]
        for chunk in chunks(posix_paths, 15):
            args = base_args + chunk
            run_cmd(msys2_root, args, cwd=repo_dir)

    cached_assets = CachedAssets()
    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with temp_pacman_conf(msys2_root) as pacman_config:
            to_add: Dict[ArchType, List[GitReleaseAsset]] = {}
            for dep_type, deps in pkg.get_depends(build_type).items():
                assets = cached_assets.get_assets(dep_type)
                for dep in deps:
                    for pattern in dep.get_build_patterns(dep_type):
                        for asset in assets:
                            if fnmatch.fnmatch(get_asset_filename(asset), pattern):
                                to_add.setdefault(dep_type, []).append(asset)
                                break
                        else:
                            if pkg.is_optional_dep(dep, dep_type):
                                # If it's there, good, if not we ignore it since it's part of a cycle
                                pass
                            else:
                                raise SystemExit(f"asset for {pattern} in {dep_type} not found")

            for dep_type, assets in to_add.items():
                add_to_repo(repo_root, pacman_config, dep_type, assets)

            with temp_pacman_script(pacman_config) as temp_pacman:
                # in case they are already installed we need to upgrade
                run_cmd(msys2_root, [to_pure_posix_path(temp_pacman), "--noconfirm", "-Suy"])
                run_cmd(msys2_root, [to_pure_posix_path(temp_pacman), "--noconfirm", "-Su"])
                yield temp_pacman
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suuy"])
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suu"])


def get_build_environ() -> Dict[str, str]:
    environ = os.environ.copy()

    # Set PACKAGER for makepkg
    packager_ref = Config.MAIN_REPO
    if "GITHUB_SHA" in environ and "GITHUB_RUN_ID" in environ:
        packager_ref += "/" + environ["GITHUB_SHA"][:8] + "/" + environ["GITHUB_RUN_ID"]
    environ["PACKAGER"] = f"CI ({packager_ref})"

    return environ


def build_package(build_type: BuildType, pkg: Package, msys2_root: _PathLike, builddir: _PathLike) -> None:
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    to_upload: List[str] = []

    repo = get_main_repo()

    with fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])

        # Fetch all keys mentioned in the PKGBUILD
        validpgpkeys = to_pure_posix_path(os.path.join(SCRIPT_DIR, 'fetch-validpgpkeys.sh'))
        run_cmd(msys2_root, ['bash', validpgpkeys], cwd=pkg_dir)

        with staging_dependencies(build_type, pkg, msys2_root, builddir) as temp_pacman:
            try:
                env = get_build_environ()
                # this makes makepkg use our custom pacman script
                env['PACMAN'] = str(to_pure_posix_path(temp_pacman))
                if build_type == "mingw-src":
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
                    ], env=env, cwd=pkg_dir)
                elif build_type in Config.MINGW_ARCH_LIST:
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
                    ], env=env, cwd=pkg_dir)
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
                failed_data = {}
                if run_urls is not None:
                    failed_data["urls"] = run_urls
                content = json.dumps(failed_data).encode()
                upload_asset(release, pkg.get_failed_name(build_type), text=True, content=content)

                raise BuildError(e)
            else:
                wait_for_api_limit_reset()
                release = repo.get_release("staging-" + build_type)
                for path in to_upload:
                    upload_asset(release, path)


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


def get_buildqueue() -> List[Package]:
    pkgs = []
    session = get_requests_session()
    r = session.get("https://packages.msys2.org/api/buildqueue2", timeout=REQUESTS_TIMEOUT)
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
    for asset in release.assets:
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


class CachedAssets:

    def __init__(self):
        self._assets = {}
        self._repos = {}
        self._failed = {}

    def _get_repo(self, build_type: BuildType) -> Repository:
        repo_name = Config.ASSETS_REPO[build_type]
        if repo_name not in self._repos:
            self._repos[repo_name] = get_github().get_repo(repo_name, lazy=True)
        return self._repos[repo_name]

    def get_assets(self, build_type: BuildType) -> List[GitReleaseAsset]:
        if build_type not in self._assets:
            repo = self._get_repo(build_type)
            release = get_release(repo, 'staging-' + build_type)
            self._assets[build_type] = get_release_assets(release)
        return self._assets[build_type]

    def get_failed_assets(self, build_type: BuildType) -> List[GitReleaseAsset]:
        repo = self._get_repo(build_type)
        key = repo.full_name
        if key not in self._failed:
            release = get_release(repo, 'staging-failed')
            self._failed[key] = get_release_assets(release)
        assets = self._failed[key]
        # XXX: This depends on the format of the filename
        return [a for a in assets if get_asset_filename(a).startswith(build_type + "-")]


def get_buildqueue_with_status(full_details: bool = False) -> List[Package]:
    cached_assets = CachedAssets()

    assets_failed = []
    for build_type in get_all_build_types():
        assets_failed.extend(cached_assets.get_failed_assets(build_type))

    failed_urls = {}
    if full_details:
        # This might take a while, so only in full mode
        with ThreadPoolExecutor(8) as executor:
            for i, (asset, content) in enumerate(
                    zip(assets_failed, executor.map(download_text_asset, assets_failed))):
                result = json.loads(content)
                if result["urls"]:
                    failed_urls[get_asset_filename(asset)] = result["urls"]

    def pkg_is_done(build_type: BuildType, pkg: Package) -> bool:
        done_names = [get_asset_filename(a) for a in cached_assets.get_assets(build_type)]
        for pattern in pkg.get_build_patterns(build_type):
            if not fnmatch.filter(done_names, pattern):
                return False
        return True

    def get_failed_urls(build_type: BuildType, pkg: Package) -> Optional[Dict[str, str]]:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        name = pkg.get_failed_name(build_type)
        if name in failed_names:
            return failed_urls.get(name)
        return None

    def pkg_has_failed(build_type: BuildType, pkg: Package) -> bool:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        name = pkg.get_failed_name(build_type)
        return name in failed_names

    def pkg_is_manual(build_type: BuildType, pkg: Package) -> bool:
        if build_type_is_src(build_type):
            return False
        for pattern, types in Config.MANUAL_BUILD:
            type_matches = not types or build_type in types
            if type_matches and fnmatch.fnmatchcase(pkg['name'], pattern):
                return True
        return False

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

                for dep_type, deps in pkg.get_depends(build_type).items():
                    for dep in deps:
                        dep_status = dep.get_status(dep_type)
                        if dep_status != PackageStatus.FINISHED:
                            if pkg.is_optional_dep(dep, dep_type):
                                continue
                            pkg.set_blocked(
                                build_type, PackageStatus.WAITING_FOR_DEPENDENCIES, dep, dep_type)

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

                    for dep_type, deps in pkg.get_depends(build_type).items():
                        for dep in deps:
                            dep_status = dep.get_status(dep_type)
                            if dep_status != PackageStatus.FINISHED:
                                pkg.set_blocked(
                                    build_type, PackageStatus.FINISHED_BUT_BLOCKED, dep, dep_type)
                                changed = True

                    for dep_type, deps in pkg.get_rdepends(build_type).items():
                        for dep in deps:
                            if dep["name"] in Config.IGNORE_RDEP_PACKAGES:
                                continue
                            dep_status = dep.get_status(dep_type)
                            dep_new = dep.is_new(dep_type)
                            # if the rdep isn't in the repo we can't break it by uploading
                            if dep_status != PackageStatus.FINISHED and not dep_new:
                                pkg.set_blocked(
                                    build_type, PackageStatus.FINISHED_BUT_BLOCKED, dep, dep_type)
                                changed = True

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
                        unfinished.append(build_type)
                else:
                    finished.append(build_type)

            # We track source packages by assuming they are in the repo if there is
            # at least one binary package in the repo. Uploading lone source
            # packages will not change anything, so block them.
            if not blocked and not unfinished and finished and \
                    all(build_type_is_src(bt) for bt in finished):
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED):
                        changed = True
                        pkg.set_status(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE)
            elif unfinished:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED):
                        changed = True
                        for bt in unfinished:
                            pkg.set_blocked(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE, pkg, bt)
            elif blocked:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status == PackageStatus.FINISHED:
                        changed = True
                        for bt in blocked:
                            pkg.set_blocked(build_type, PackageStatus.FINISHED_BUT_BLOCKED, pkg, bt)

    return pkgs


def get_cycles(pkgs: List[Package]) -> Set[Tuple[Package, Package]]:
    cycles: Set[Tuple[Package, Package]] = set()

    # In case the package is already built it doesn't matter if it is part of a cycle
    def pkg_is_finished(pkg: Package, build_type: BuildType) -> bool:
        return pkg.get_status(build_type) in [
            PackageStatus.FINISHED,
            PackageStatus.FINISHED_BUT_BLOCKED,
            PackageStatus.FINISHED_BUT_INCOMPLETE,
        ]

    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if build_type_is_src(build_type):
                continue
            if pkg_is_finished(pkg, build_type):
                continue
            build_type = cast(ArchType, build_type)
            for dep_type, deps in pkg.get_depends(build_type).items():
                for dep in deps:
                    if pkg_is_finished(dep, dep_type):
                        continue
                    # manually broken cycle
                    if pkg.is_optional_dep(dep, dep_type) or dep.is_optional_dep(pkg, build_type):
                        continue
                    dep_deps = dep.get_depends(dep_type)
                    if pkg in dep_deps.get(build_type, set()):
                        cycles.add(tuple(sorted([pkg, dep], key=lambda p: p["name"])))  # type: ignore
    return cycles


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


def get_workflow() -> Workflow:
    workflow_name = os.environ.get("GITHUB_WORKFLOW", None)
    if workflow_name is None:
        raise Exception("GITHUB_WORKFLOW not set")
    repo = get_main_repo()
    for workflow in repo.get_workflows():
        if workflow.name == workflow_name:
            return workflow
    else:
        raise Exception("workflow not found:", workflow_name)


def get_job_meta() -> List[Dict[str, Any]]:
    hosted_runner = "windows-2022"
    job_meta: List[Dict[str, Any]] = [
        {
            "build-types": ["mingw64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types mingw64",
                "name": "mingw64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["mingw32"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types mingw32",
                "name": "mingw32",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["ucrt64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types ucrt64",
                "name": "ucrt64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clang64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clang64",
                "name": "clang64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clang32"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clang32",
                "name": "clang32",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clangarm64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clangarm64",
                "name": "clangarm64",
                "runner": ["Windows", "ARM64", "autobuild"]
            }
        }, {
            "build-types": ["msys"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types msys",
                "name": "msys",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["msys-src", "mingw-src"],
            "matrix": {
                "packages": "base-devel VCS",
                "build-args": "--build-types msys-src,mingw-src",
                "name": "src",
                "runner": hosted_runner
            }
        }
    ]

    return job_meta


def parse_optional_deps(optional_deps: str) -> Dict[str, List[str]]:
    res: Dict[str, List[str]] = {}
    optional_deps = optional_deps.replace(" ", "")
    if not optional_deps:
        return res
    for entry in optional_deps.split(","):
        assert ":" in entry
        first, second = entry.split(":", 2)
        res.setdefault(first, []).append(second)
    return res


def apply_optional_deps(optional_deps: str) -> None:
    for dep, ignored in parse_optional_deps(optional_deps).items():
        Config.OPTIONAL_DEPS.setdefault(dep, []).extend(ignored)


def write_build_plan(args: Any) -> None:
    target_file = args.target_file
    optional_deps = args.optional_deps or ""

    apply_optional_deps(optional_deps)

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

    show_cycles(pkgs)

    update_status(pkgs)

    queued_build_types: Dict[str, int] = {}
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                queued_build_types[build_type] = queued_build_types.get(build_type, 0) + 1

    if not queued_build_types:
        write_out([])
        return

    available_build_types = set()
    for build_type, repo_name in Config.ASSETS_REPO.items():
        if repo_name == Config.MAIN_REPO:
            available_build_types.add(build_type)

    jobs = []
    for job_info in get_job_meta():
        matching_build_types = \
            set(queued_build_types) & set(job_info["build-types"]) & available_build_types
        if matching_build_types:
            build_count = sum(queued_build_types[bt] for bt in matching_build_types)
            job = job_info["matrix"]
            # TODO: pin optional deps to their exact version somehow, in case something changes
            # between this run and when the worker gets to it.
            if optional_deps:
                job["build-args"] = job["build-args"] + " --optional-deps " + shlex.quote(optional_deps)
            jobs.append(job)
            # XXX: If there is more than three builds we start two jobs with the second
            # one having a reversed build order
            if build_count > 3:
                matrix = dict(job)
                matrix["build-args"] = matrix["build-args"] + " --build-from end"
                matrix["name"] = matrix["name"] + "-2"
                jobs.append(matrix)
            if build_count > 9:
                matrix = dict(job)
                matrix["build-args"] = matrix["build-args"] + " --build-from middle"
                matrix["name"] = matrix["name"] + "-3"
                jobs.append(matrix)

    write_out(jobs[:Config.MAXIMUM_JOB_COUNT])


def queue_website_update() -> None:
    session = get_requests_session()
    r = session.post('https://packages.msys2.org/api/trigger_update', timeout=REQUESTS_TIMEOUT)
    try:
        # it's not worth stopping the build if this fails, so just log it
        r.raise_for_status()
    except requests.RequestException as e:
        print(e)


def update_status(pkgs: List[Package]) -> None:
    repo = get_main_repo()
    release = get_release(repo, "status")

    status_object: Dict[str, Any] = {}

    packages = []
    for pkg in pkgs:
        pkg_result = {}
        pkg_result["name"] = pkg["name"]
        pkg_result["version"] = pkg["version"]
        builds = {}
        for build_type in pkg.get_build_types():
            details = pkg.get_status_details(build_type)
            details.pop("blocked", None)
            details["status"] = pkg.get_status(build_type).value
            builds[build_type] = details
        pkg_result["builds"] = builds
        packages.append(pkg_result)
    status_object["packages"] = packages

    cycles = []
    for a, b in get_cycles(pkgs):
        cycles.append([a["name"], b["name"]])
    status_object["cycles"] = sorted(cycles)

    content = json.dumps(status_object, indent=2).encode()

    # If multiple jobs update this at the same time things can fail
    try:
        asset_name = "status.json"
        for asset in release.assets:
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

    print(f"Uploaded status file for {len(packages)} packages: {new_asset.browser_download_url}")

    queue_website_update()


def run_update_status(args: Any) -> None:
    update_status(get_buildqueue_with_status(full_details=True))


def show_cycles(pkgs: List[Package]) -> None:
    cycles = get_cycles(pkgs)
    if cycles:
        def format_package(p: Package) -> str:
            return f"{p['name']} [{p['version_repo']} -> {p['version']}]"

        with gha_group(f"Dependency Cycles ({len(cycles)})"):
            print(tabulate([
                (format_package(a), "<-->", format_package(b)) for (a, b) in cycles],
                headers=["Package", "", "Package"]))


def show_build(args: Any) -> None:
    todo = []
    waiting = []
    done = []
    failed = []

    apply_optional_deps(args.optional_deps or "")

    pkgs = get_buildqueue_with_status(full_details=args.details)

    show_cycles(pkgs)

    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            details = pkg.get_status_details(build_type)
            details.pop("blocked", None)
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


def get_repo_subdir(build_type: BuildType) -> Path:
    if build_type == "msys":
        return Path("msys") / "x86_64"
    elif build_type == "msys-src":
        return Path("msys") / "sources"
    elif build_type == "mingw-src":
        return Path("mingw") / "sources"
    elif build_type in Config.MINGW_ARCH_LIST:
        return Path("mingw") / build_type
    else:
        raise Exception("unknown type")


def upload_assets(args: Any) -> None:
    repo = get_main_repo()
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
        release = get_release(repo, 'staging-' + build_type)
        print(f"Uploading {match}")
        if not args.dry_run:
            upload_asset(release, match)
    print("Done")


def fetch_assets(args: Any) -> None:
    target_dir = os.path.abspath(args.targetdir)
    fetch_all = args.fetch_all
    fetch_complete = args.fetch_complete

    all_patterns: Dict[BuildType, List[str]] = {}
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
    assets_to_download: Dict[BuildType, List[GitReleaseAsset]] = {}
    for build_type, patterns in all_patterns.items():
        if build_type not in all_assets:
            all_assets[build_type] = cached_assets.get_assets(build_type)
        assets = all_assets[build_type]

        assets_mapping: Dict[str, List[GitReleaseAsset]] = {}
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
    print("Pass --fetch-complete to also fetch blocked but complete packages")
    print("Pass --fetch-all to fetch all packages.")
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


def get_all_build_types() -> List[BuildType]:
    all_build_types: List[BuildType] = ["msys", "msys-src", "mingw-src"]
    all_build_types.extend(Config.MINGW_ARCH_LIST)
    return all_build_types


def get_assets_to_delete(repo: Repository) -> List[GitReleaseAsset]:
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
    repo = get_main_repo()
    assets = get_assets_to_delete(repo)

    def delete_asset(asset: GitReleaseAsset) -> None:
        print(f"Deleting {get_asset_filename(asset)}...")
        if not args.dry_run:
            with make_writable(asset):
                asset.delete_asset()

    with ThreadPoolExecutor(4) as executor:
        for item in executor.map(delete_asset, assets):
            pass


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


def get_credentials(readonly: bool = True) -> Dict[str, Any]:
    if readonly and os.environ.get("GITHUB_TOKEN_READONLY", ""):
        return {'login_or_token': os.environ["GITHUB_TOKEN_READONLY"]}
    elif "GITHUB_TOKEN" in os.environ:
        return {'login_or_token': os.environ["GITHUB_TOKEN"]}
    else:
        if readonly:
            print("[Warning] 'GITHUB_TOKEN' or 'GITHUB_TOKEN_READONLY' env vars "
                  "not set which might lead to API rate limiting", file=sys.stderr)
            return {}
        else:
            raise Exception("'GITHUB_TOKEN' env var not set")


@lru_cache(maxsize=None)
def get_github(readonly: bool = True) -> Github:
    kwargs = get_credentials(readonly=readonly)
    has_creds = bool(kwargs)
    # 100 is the maximum allowed
    kwargs['per_page'] = 100
    kwargs['retry'] = REQUESTS_RETRY
    kwargs['timeout'] = sum(REQUESTS_TIMEOUT)
    gh = Github(**kwargs)
    if not has_creds and readonly:
        print(f"[Warning] Rate limit status: {gh.get_rate_limit().core}", file=sys.stderr)
    return gh


@lru_cache(maxsize=None)
def get_main_repo(readonly: bool = True) -> Repository:
    gh = get_github(readonly=readonly)
    return gh.get_repo(Config.MAIN_REPO, lazy=True)


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
            print(f"Too few API calls left, waiting for {wait} seconds")
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


@contextmanager
def install_requests_cache() -> Generator:
    # This adds basic etag based caching, to avoid hitting API rate limiting

    import requests_cache
    from requests_cache.backends.sqlite import SQLiteCache

    # Monkey patch globally, so pygithub uses it as well.
    # Only do re-validation with etag/date etc and ignore the cache-control headers that
    # github sends by default with 60 seconds.
    cache_dir = os.path.join(SCRIPT_DIR, '.autobuild_cache')
    os.makedirs(cache_dir, exist_ok=True)
    requests_cache.install_cache(
        always_revalidate=True,
        cache_control=False,
        expire_after=requests_cache.EXPIRE_IMMEDIATELY,
        backend=SQLiteCache(os.path.join(cache_dir, 'http_cache.sqlite')))

    # Call this once, so it gets cached from the main thread and can be used in a thread pool
    get_requests_session(nocache=True)

    try:
        yield
    finally:
        # Delete old cache entries, so this doesn't grow indefinitely
        cache = requests_cache.get_cache()
        assert cache is not None
        cache.delete(older_than=timedelta(hours=3))

        # un-monkey-patch again
        requests_cache.uninstall_cache()


def requests_cache_disabled() -> Any:
    import requests_cache
    return requests_cache.disabled()


def main(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.set_defaults(func=lambda *x: parser.print_help())
    parser.add_argument(
           "-R", "--repo", action="store",
           help=f"msys2-autobuild repository to target (default '{Config.MAIN_REPO}')",
           default=Config.MAIN_REPO)
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("-t", "--build-types", action="store")
    sub.add_argument(
        "--build-from", action="store", default="start", help="Start building from start|end|middle")
    sub.add_argument("--optional-deps", action="store")
    sub.add_argument("msys2_root", help="The MSYS2 install used for building. e.g. C:\\msys64")
    sub.add_argument(
        "builddir",
        help="A directory used for saving temporary build results and the git repos")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.add_argument(
        "--details", action="store_true", help="Show more details such as links to failed build logs (slow)")
    sub.add_argument("--optional-deps", action="store")
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser(
        "write-build-plan", help="Write a GHA build matrix setup", allow_abbrev=False)
    sub.add_argument("--optional-deps", action="store")
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
    sub.add_argument(
        "--fetch-complete", action="store_true",
        help="Fetch all packages, even blocked ones, except incomplete ones")
    sub.add_argument(
        "-t", "--build-type", action="append",
        help="Only fetch packages for given build type(s) (may be used more than once)")
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
        "clear-failed", help="Clear the failed state for packages", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.add_argument("--build-types", action="store", help=(
        "A comma separated list of build types (e.g. mingw64)"))
    sub.add_argument("--packages", action="store", help=(
        "A comma separated list of packages to clear (e.g. mingw-w64-qt-creator)"))
    sub.set_defaults(func=clear_failed_state)

    args = parser.parse_args(argv[1:])
    Config.MAIN_REPO = args.repo
    with install_requests_cache():
        args.func(args)


if __name__ == "__main__":
    main(sys.argv)
