#!/usr/bin/env python3

import sys
import os
import argparse
import glob
from os import environ
from github import Github
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset
from github.Repository import Repository
from pathlib import Path, PurePosixPath, PurePath
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
import json
import io
from enum import Enum
from hashlib import sha256
from typing import Generator, Union, AnyStr, List, Any, Dict, Tuple, Set, Optional

_PathLike = Union[os.PathLike, AnyStr]

# Feel free to add yourself here if you have permissions
ALLOWED_UPLOADERS = [
    ("User", "elieux"),
    ("User", "Alexpux"),
    ("User", "lazka"),
    ("Bot", "github-actions[bot]"),
]


class PackageStatus(Enum):
    FINISHED = 'finished'
    FINISHED_BUT_BLOCKED = 'finished-but-blocked'
    FINISHED_BUT_INCOMPLETE = 'finished-but-incomplete'
    FAILED_TO_BUILD = 'failed-to-build'
    WAITING_FOR_BUILD = 'waiting-for-build'
    WAITING_FOR_DEPENDENCIES = 'waiting-for-dependencies'
    MANUAL_BUILD_REQUIRED = 'manual-build-required'
    UNKNOWN = 'unknown'

    def __str__(self):
        return self.value


class Package(dict):

    def __repr__(self):
        return "Package(%r)" % self["name"]

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other

    def get_status(self, build_type: str) -> PackageStatus:
        return self.get("status", {}).get(build_type, PackageStatus.UNKNOWN)

    def get_status_details(self, build_type: str) -> Dict[str, str]:
        return self.get("status_details", {}).get(build_type, {})

    def set_status(self, build_type: str, status: PackageStatus,
                   description: Optional[str] = None, url: Optional[str] = None) -> None:
        self.setdefault("status", {})[build_type] = status
        meta = {}
        if description:
            meta["desc"] = description
        if url:
            meta["url"] = url
        self.setdefault("status_details", {})[build_type] = meta

    def get_build_patterns(self, build_type: str) -> List[str]:
        patterns = []
        if build_type in ["mingw-src", "msys-src"]:
            patterns.append(f"{self['name']}-{self['version']}.src.tar.*")
        elif build_type in ["mingw32", "mingw64", "msys"]:
            for item in self['packages'].get(build_type, []):
                patterns.append(f"{item}-{self['version']}-*.pkg.tar.*")
        else:
            assert 0
        return patterns

    def get_failed_names(self, build_type: str) -> List[str]:
        names = []
        if build_type in ["mingw-src", "msys-src"]:
            names.append(f"{self['name']}-{self['version']}.failed")
        elif build_type in ["mingw32", "mingw64", "msys"]:
            for item in self['packages'].get(build_type, []):
                names.append(f"{item}-{self['version']}.failed")
        else:
            assert 0
        return names

    def get_build_types(self) -> List[str]:
        build_types = list(self["packages"].keys())
        if any(k.startswith("mingw") for k in self["packages"].keys()):
            build_types.append("mingw-src")
        if "msys" in self["packages"].keys():
            build_types.append("msys-src")
        return build_types

    def get_repo_type(self) -> str:
        return "msys" if self['repo'].startswith('MSYS2') else "mingw"


# After which we shouldn't start a new build
SOFT_TIMEOUT = 60 * 60 * 3

# Packages that take too long to build, and should be handled manually
MANUAL_BUILD: List[str] = [
    'mingw-w64-firebird-git',
    'mingw-w64-qt5-static',
]

# FIXME: Packages that should be ignored if they depend on other things
# in the queue. Ideally this list should be empty..
IGNORE_RDEP_PACKAGES: List[str] = [
    "mingw-w64-mlpack",
    "mingw-w64-qemu",
    "mingw-w64-ghc",
    "mingw-w64-python-notebook",
    "mingw-w64-python-pywin32",
    "mingw-w64-usbmuxd",
    "mingw-w64-npm",
    "mingw-w64-yarn",
    "mingw-w64-bower",
    "mingw-w64-nodejs",
]

REPO = "msys2/msys2-autobuild"


def get_current_run_url() -> Optional[str]:
    if "GITHUB_RUN_ID" in os.environ and "GITHUB_REPOSITORY" in os.environ:
        run_id = os.environ["GITHUB_RUN_ID"]
        repo = os.environ["GITHUB_REPOSITORY"]
        return f"https://github.com/{repo}/actions/runs/{run_id}"
    return None


def run_cmd(msys2_root: _PathLike, args, **kwargs):
    executable = os.path.join(msys2_root, 'usr', 'bin', 'bash.exe')
    env = kwargs.pop("env", os.environ.copy())
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    env["MSYS2_PATH_TYPE"] = "minimal"
    check_call([executable, '-lc'] + [shlex.join([str(a) for a in args])], env=env, **kwargs)


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


def download_asset(asset: GitReleaseAsset, target_path: str) -> None:
    assert asset_is_complete(asset)
    with requests.get(asset.browser_download_url, stream=True, timeout=(15, 30)) as r:
        r.raise_for_status()
        fd, temppath = tempfile.mkstemp()
        try:
            os.chmod(temppath, 0o644)
            with os.fdopen(fd, "wb") as h:
                for chunk in r.iter_content(4096):
                    h.write(chunk)
            shutil.move(temppath, target_path)
        finally:
            try:
                os.remove(temppath)
            except OSError:
                pass


def download_text_asset(asset: GitReleaseAsset) -> str:
    assert asset_is_complete(asset)
    with requests.get(asset.browser_download_url, timeout=(15, 30)) as r:
        r.raise_for_status()
        return r.text


def upload_asset(release: GitRelease, path: _PathLike, replace: bool = False,
                 text: bool = False, content: bytes = None) -> None:
    path = Path(path)
    basename = os.path.basename(str(path))
    asset_name = get_gh_asset_name(basename, text)
    asset_label = basename

    for asset in get_release_assets(release, include_incomplete=True):
        if asset_name == asset.name:
            # We want to tread incomplete assets as if they weren't there
            # so replace them always
            if replace or not asset_is_complete(asset):
                asset.delete_asset()
            else:
                print(f"Skipping upload for {asset_name} as {asset_label}, already exists")
                return

    if content is None:
        release.upload_asset(str(path), label=asset_label, name=asset_name)
    else:
        with io.BytesIO(content) as fileobj:
            release.upload_asset_from_memory(  # type: ignore
                fileobj, len(content), label=asset_label, name=asset_name)
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
def auto_key_retrieve(msys2_root: _PathLike) -> Generator:
    home_dir = os.path.join(msys2_root, "home", environ["USERNAME"])
    assert os.path.exists(home_dir)
    gnupg_dir = os.path.join(home_dir, ".gnupg")
    os.makedirs(gnupg_dir, exist_ok=True)
    conf = os.path.join(gnupg_dir, "gpg.conf")
    backup = None
    if os.path.exists(conf):
        backup = conf + ".backup"
        shutil.copyfile(conf, backup)
    try:
        with open(conf, "w", encoding="utf-8") as h:
            h.write("""
keyserver hkp://keys.gnupg.net
keyserver-options auto-key-retrieve
""")
        yield
    finally:
        if backup is not None:
            os.replace(backup, conf)


def build_type_to_dep_types(build_type: str) -> List[str]:
    if build_type == "mingw-src":
        build_type = "mingw64"
    elif build_type == "msys-src":
        build_type = "msys"

    if build_type == "msys":
        return [build_type]
    else:
        return ["msys", build_type]


def build_type_to_rdep_types(build_type: str) -> List[str]:
    if build_type == "mingw-src":
        build_type = "mingw64"
    elif build_type == "msys-src":
        build_type = "msys"

    if build_type == "msys":
        return [build_type, "mingw32", "mingw64"]
    else:
        return [build_type]


@contextmanager
def staging_dependencies(
        build_type: str, pkg: Package, msys2_root: _PathLike,
        builddir: _PathLike) -> Generator:
    repo = get_repo()

    def add_to_repo(repo_root, repo_type, asset):
        repo_dir = Path(repo_root) / get_repo_subdir(repo_type, asset)
        os.makedirs(repo_dir, exist_ok=True)
        print(f"Downloading {get_asset_filename(asset)}...")
        package_path = os.path.join(repo_dir, get_asset_filename(asset))
        download_asset(asset, package_path)

        repo_name = "autobuild-" + (
            str(get_repo_subdir(repo_type, asset)).replace("/", "-").replace("\\", "-"))
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

        run_cmd(msys2_root, ["repo-add", to_pure_posix_path(repo_db_path),
                             to_pure_posix_path(package_path)], cwd=repo_dir)

    def get_cached_assets(
            repo: Repository, release_name: str, *, _cache={}) -> List[GitReleaseAsset]:
        key = (repo.full_name, release_name)
        if key not in _cache:
            release = repo.get_release(release_name)
            _cache[key] = get_release_assets(release)
        return _cache[key]

    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with backup_pacman_conf(msys2_root):
            to_add = []
            for dep_type in build_type_to_dep_types(build_type):
                for dep in pkg['ext-depends'].get(dep_type, set()):
                    repo_type = dep.get_repo_type()
                    assets = get_cached_assets(repo, "staging-" + repo_type)
                    for pattern in dep.get_build_patterns(dep_type):
                        for asset in assets:
                            if fnmatch.fnmatch(get_asset_filename(asset), pattern):
                                to_add.append((repo_type, asset))
                                break
                        else:
                            raise SystemExit(f"asset for {pattern} in {repo_type} not found")

            for repo_type, asset in to_add:
                add_to_repo(repo_root, repo_type, asset)

            # in case they are already installed we need to upgrade
            run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suy"])
            yield
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suuy"])


def build_package(build_type: str, pkg, msys2_root: _PathLike, builddir: _PathLike) -> None:
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    to_upload: List[str] = []

    repo = get_repo()

    with staging_dependencies(build_type, pkg, msys2_root, builddir), \
            auto_key_retrieve(msys2_root), \
            fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])

        try:
            if build_type == "mingw-src":
                env = environ.copy()
                env['MINGW_INSTALLS'] = 'mingw64'
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
            elif build_type in ["mingw32", "mingw64"]:
                env = environ.copy()
                env['MINGW_INSTALLS'] = build_type
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
            release = repo.get_release("staging-failed")
            for entry in pkg.get_failed_names(build_type):
                failed_data = {}
                run_url = get_current_run_url()
                if run_url is not None:
                    failed_data["url"] = run_url
                content = json.dumps(failed_data).encode()
                upload_asset(release, entry, text=True, content=content)

            raise BuildError(e)
        else:
            release = repo.get_release("staging-" + pkg.get_repo_type())
            for path in to_upload:
                upload_asset(release, path)


def run_build(args: Any) -> None:
    builddir = os.path.abspath(args.builddir)
    msys2_root = os.path.abspath(args.msys2_root)
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

    while True:
        pkgs = get_buildqueue_with_status(full_details=True)
        update_status(pkgs)
        todo = get_package_to_build(pkgs)
        if not todo:
            break
        pkg, build_type = todo

        if (time.monotonic() - start_time) >= SOFT_TIMEOUT:
            print("timeout reached")
            break

        try:
            with gha_group(f"[{ pkg['repo'] }] [{ build_type }] { pkg['name'] }..."):
                build_package(build_type, pkg, msys2_root, builddir)
        except BuildError:
            with gha_group(f"[{ pkg['repo'] }] [{ build_type }] { pkg['name'] }: failed"):
                traceback.print_exc(file=sys.stdout)
            continue


def get_buildqueue() -> List[Package]:
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue")
    r.raise_for_status()
    dep_mapping = {}
    for received in r.json():
        pkg = Package(received)
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)
        for repo, names in pkg['packages'].items():
            for name in names:
                dep_mapping[name] = pkg

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        ver_depends: Dict[str, Set[Package]] = {}
        for repo, deps in pkg['depends'].items():
            for dep in deps:
                ver_depends.setdefault(repo, set()).add(dep_mapping[dep])
        pkg['ext-depends'] = ver_depends

    # reverse dependencies
    for pkg in pkgs:
        r_depends: Dict[str, Set[Package]] = {}
        for pkg2 in pkgs:
            for repo, deps in pkg2['ext-depends'].items():
                if pkg in deps:
                    for r_repo in pkg2['packages'].keys():
                        r_depends.setdefault(r_repo, set()).add(pkg2)
        pkg['ext-rdepends'] = r_depends

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


def get_release_assets(release: GitRelease, include_incomplete=False) -> List[GitReleaseAsset]:
    assets = []
    for asset in release.get_assets():
        # skip in case not fully uploaded yet (or uploading failed)
        if not asset_is_complete(asset) and not include_incomplete:
            continue
        uploader = asset.uploader
        uploader_key = (uploader.type, uploader.login)
        # We allow uploads from some users and GHA
        if uploader_key not in ALLOWED_UPLOADERS:
            raise SystemExit(
                f"ERROR: Asset '{get_asset_filename(asset)}' "
                f"uploaded by {uploader_key}'. Aborting.")
        assets.append(asset)
    return assets


def get_buildqueue_with_status(full_details: bool = False) -> List[Package]:
    repo = get_repo(optional_credentials=True)
    assets = []
    for name in ["msys", "mingw"]:
        release = repo.get_release('staging-' + name)
        assets.extend(get_release_assets(release))
    release = repo.get_release('staging-failed')
    assets_failed = get_release_assets(release)

    failed_urls = {}
    if full_details:
        # This might take a while, so only in full mode
        with ThreadPoolExecutor(8) as executor:
            for i, (asset, content) in enumerate(
                    zip(assets_failed, executor.map(download_text_asset, assets_failed))):
                result = json.loads(content)
                if result["url"]:
                    failed_urls[get_asset_filename(asset)] = result["url"]

    def pkg_is_done(build_type: str, pkg: Package) -> bool:
        done_names = [get_asset_filename(a) for a in assets]
        for pattern in pkg.get_build_patterns(build_type):
            if not fnmatch.filter(done_names, pattern):
                return False
        return True

    def get_failed_url(build_type: str, pkg: Package) -> Optional[str]:
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
        if build_type in ["mingw-src", "msys-src"]:
            return False
        return pkg['name'] in MANUAL_BUILD

    pkgs = get_buildqueue()

    # basic state
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg_is_done(build_type, pkg):
                pkg.set_status(build_type, PackageStatus.FINISHED)
            elif pkg_has_failed(build_type, pkg):
                url = get_failed_url(build_type, pkg)
                pkg.set_status(build_type, PackageStatus.FAILED_TO_BUILD, url=url)
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
                for dep_type in build_type_to_dep_types(build_type):
                    for dep in pkg['ext-depends'].get(dep_type, set()):
                        dep_status = dep.get_status(dep_type)
                        if dep_status != PackageStatus.FINISHED:
                            missing_deps.add(dep)
                if missing_deps:
                    desc = f"Waiting for: {', '.join(sorted(d['name'] for d in missing_deps))}"
                    pkg.set_status(build_type, PackageStatus.WAITING_FOR_DEPENDENCIES, desc)

    # Block packages where not every build type is finished
    for pkg in pkgs:
        unfinished = []
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            if status != PackageStatus.FINISHED:
                unfinished.append(build_type)
        if unfinished:
            for build_type in pkg.get_build_types():
                status = pkg.get_status(build_type)
                if status == PackageStatus.FINISHED:
                    desc = f"Missing related builds: {', '.join(sorted(unfinished))}"
                    pkg.set_status(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE, desc)

    # Block packages where not all deps/rdeps are finished
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            if status == PackageStatus.FINISHED:
                missing_deps = set()
                for dep_type in build_type_to_dep_types(build_type):
                    for dep in pkg['ext-depends'].get(dep_type, set()):
                        dep_status = dep.get_status(dep_type)
                        if dep_status not in (PackageStatus.FINISHED,
                                              PackageStatus.FINISHED_BUT_BLOCKED):
                            missing_deps.add(dep)

                missing_rdeps = set()
                for dep_type in build_type_to_rdep_types(build_type):
                    for dep in pkg['ext-rdepends'].get(dep_type, set()):
                        dep_status = dep.get_status(dep_type)
                        if dep["name"] in IGNORE_RDEP_PACKAGES:
                            continue
                        if dep_status not in (PackageStatus.FINISHED,
                                              PackageStatus.FINISHED_BUT_BLOCKED):
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
                    pkg.set_status(build_type, PackageStatus.FINISHED_BUT_BLOCKED, ". ".join(descs))

    return pkgs


def get_package_to_build(pkgs: List[Package]) -> Optional[Tuple[Package, str]]:
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                return (pkg, build_type)
    return None


def get_workflow():
    workflow_name = os.environ.get("GITHUB_WORKFLOW", None)
    if workflow_name is None:
        raise Exception("GITHUB_WORKFLOW not set")
    repo = get_repo()
    for workflow in repo.get_workflows():
        if workflow.name == workflow_name:
            return workflow
    else:
        raise Exception("workflow not found:", workflow_name)


def should_run(args: Any) -> None:
    current_id = None
    if "GITHUB_RUN_ID" in os.environ:
        current_id = int(os.environ["GITHUB_RUN_ID"])

    workflow = get_workflow()
    runs = list(workflow.get_runs(status="in_progress"))
    runs += list(workflow.get_runs(status="queued"))
    for run in runs:
        if current_id is not None and current_id == run.id:
            # Ignore this run itself
            continue
        raise SystemExit(
            f"Another workflow is currently running or has something queued: {run.html_url}")

    pkgs = get_buildqueue_with_status(full_details=True)
    update_status(pkgs)
    if not get_package_to_build(pkgs):
        raise SystemExit("Nothing to build")


def update_status(pkgs: List[Package]):
    repo = get_repo()
    release = repo.get_release("status")

    results = {}
    for pkg in pkgs:
        pkg_result = {}
        for build_type in pkg.get_build_types():
            details = pkg.get_status_details(build_type)
            details["status"] = pkg.get_status(build_type).value
            pkg_result[build_type] = details
        results[pkg["name"]] = pkg_result

    content = json.dumps(results, indent=2).encode()

    asset_name = "status.json"
    for asset in release.get_assets():
        if asset.name == asset_name:
            asset.delete_asset()
            break
    with io.BytesIO(content) as fileobj:
        new_asset = release.upload_asset_from_memory(  # type: ignore
            fileobj, len(content), asset_name)

    print(f"Uploaded status file for {len(results)} packages: {new_asset.browser_download_url}")


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

    def show_table(name, items):
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
        else:
            raise Exception("unknown file type")
    else:
        raise Exception("unknown type")


def upload_assets(args: Any) -> None:
    repo = get_repo()
    package_name = args.package_name
    src_dir = args.path
    if src_dir is None:
        src_dir = os.getcwd()
    src_dir = os.path.abspath(src_dir)

    for pkg in get_buildqueue_with_status():
        if pkg["name"] == package_name:
            break
    else:
        raise SystemExit(f"Package '{package_name}' not in the queue, check the 'show' command")

    patterns = []
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

    release = repo.get_release('staging-' + repo_type)
    for match in matches:
        print(f"Uploading {match}")
        if not args.dry_run:
            upload_asset(release, match)
    print("Done")


def fetch_assets(args: Any) -> None:
    repo = get_repo(optional_credentials=True)
    target_dir = args.targetdir
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
            elif status == PackageStatus.FINISHED_BUT_BLOCKED:
                if fetch_all:
                    all_patterns.setdefault(repo_type, []).extend(pkg_patterns)
                else:
                    all_blocked.append(
                        (pkg["name"], build_type, pkg.get_status_details(build_type)))

    all_assets = {}
    to_download: Dict[str, List[GitReleaseAsset]] = {}
    for repo_type, patterns in all_patterns.items():
        if repo_type not in all_assets:
            release = repo.get_release('staging-' + repo_type)
            all_assets[repo_type] = get_release_assets(release)
        assets = all_assets[repo_type]

        assets_mapping: Dict[str, List[GitReleaseAsset]] = {}
        for asset in assets:
            assets_mapping.setdefault(get_asset_filename(asset), []).append(asset)

        for pattern in patterns:
            matches = fnmatch.filter(assets_mapping.keys(), pattern)
            if matches:
                found = assets_mapping[matches[0]]
                to_download.setdefault(repo_type, []).extend(found)

    todo = []
    done = []
    for repo_type, assets in to_download.items():
        for asset in assets:
            asset_dir = Path(target_dir) / get_repo_subdir(repo_type, asset)
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path = asset_dir / get_asset_filename(asset)
            if asset_path.exists():
                if asset_path.stat().st_size != asset.size:
                    print(f"Warning: {asset_path} already exists "
                          f"but has a different size")
                done.append(asset)
                continue
            todo.append((asset, asset_path))

    if args.verbose and all_blocked:
        import pprint
        print("Packages that are blocked and why:")
        pprint.pprint(all_blocked)

    print(f"downloading: {len(todo)}, done: {len(done)}, "
          f"blocked: {len(all_blocked)} (related builds missing)")

    print("Pass --verbose to see the list of blocked packages.")
    print("Pass --fetch-all to also fetch blocked packages.")

    def fetch_item(item):
        asset, asset_path = item
        if not args.pretend:
            download_asset(asset, asset_path)
        return item

    with ThreadPoolExecutor(8) as executor:
        for i, item in enumerate(executor.map(fetch_item, todo)):
            print(f"[{i + 1}/{len(todo)}] {get_asset_filename(item[0])}")

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
        release = repo.get_release(release_name)
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
            asset.delete_asset()

    if not assets:
        print("Nothing to delete")


def get_credentials(optional: bool = False) -> Dict[str, Any]:
    if "GITHUB_TOKEN" in environ:
        return {'login_or_token': environ["GITHUB_TOKEN"]}
    elif "GITHUB_USER" in environ and "GITHUB_PASS" in environ:
        return {'login_or_token': environ["GITHUB_USER"], 'password': environ["GITHUB_PASS"]}
    else:
        if optional:
            print("[Warning] 'GITHUB_TOKEN' or 'GITHUB_USER'/'GITHUB_PASS' env vars "
                  "not set which might lead to API rate limiting", file=sys.stderr)
            return {}
        else:
            raise Exception("'GITHUB_TOKEN' or 'GITHUB_USER'/'GITHUB_PASS' env vars not set")


def get_repo(optional_credentials: bool = False) -> Repository:
    kwargs = get_credentials(optional=optional_credentials)
    has_creds = bool(kwargs)
    # 100 is the maximum allowed
    kwargs['per_page'] = 100
    gh = Github(**kwargs)
    if not has_creds and optional_credentials:
        print(f"[Warning] Rate limit status: {gh.get_rate_limit().core}", file=sys.stderr)
    return gh.get_repo(REPO, lazy=True)


def main(argv: List[str]):
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.set_defaults(func=lambda *x: parser.print_help())
    subparser = parser.add_subparsers(title="subcommands")

    sub = subparser.add_parser("build", help="Build all packages")
    sub.add_argument("msys2_root", help="The MSYS2 install used for building. e.g. C:\\msys64")
    sub.add_argument(
        "builddir",
        help="A directory used for saving temporary build results and the git repos")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser(
        "show", help="Show all packages to be built", allow_abbrev=False)
    sub.add_argument(
        "--fail-on-idle", action="store_true", help="Fails if there is nothing to do")
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser(
        "should-run", help="Fails if the workflow shouldn't run", allow_abbrev=False)
    sub.set_defaults(func=should_run)

    sub = subparser.add_parser(
        "update-status", help="Update the status file", allow_abbrev=False)
    sub.set_defaults(func=run_update_status)

    sub = subparser.add_parser(
        "fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
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
    sub.add_argument("package_name")
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be uploaded")
    sub.add_argument("-p", "--path", action="store", help=(
        "Directory to look for packages in "
        "(defaults to the current working directory by default)"))
    sub.set_defaults(func=upload_assets)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)
