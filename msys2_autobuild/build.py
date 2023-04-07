import fnmatch
import json
import os
import time
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path, PurePath, PurePosixPath
from subprocess import check_call
from typing import Any, Dict, Generator, List, Sequence, TypeVar, Tuple

from github.GitReleaseAsset import GitReleaseAsset

from .config import ArchType, BuildType, Config
from .gh import (CachedAssets, download_asset, get_asset_filename,
                 get_current_run_urls, get_release, get_repo, upload_asset,
                 wait_for_api_limit_reset)
from .queue import Package
from .utils import SCRIPT_DIR, PathLike


class BuildError(Exception):
    pass


def get_python_path(msys2_root: PathLike, msys2_path: PathLike) -> Path:
    return Path(os.path.normpath(str(msys2_root) + str(msys2_path)))


def to_pure_posix_path(path: PathLike) -> PurePath:
    return PurePosixPath("/" + str(path).replace(":", "", 1).replace("\\", "/"))


def get_build_environ(build_type: BuildType) -> Dict[str, str]:
    environ = os.environ.copy()

    # Set PACKAGER for makepkg
    packager_ref = Config.ASSETS_REPO[build_type]
    if "GITHUB_SHA" in environ and "GITHUB_RUN_ID" in environ:
        packager_ref += "/" + environ["GITHUB_SHA"][:8] + "/" + environ["GITHUB_RUN_ID"]
    environ["PACKAGER"] = f"CI ({packager_ref})"

    return environ


@contextmanager
def temp_pacman_script(pacman_config: PathLike) -> Generator[PathLike, None, None]:
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
def temp_pacman_conf(msys2_root: PathLike) -> Generator[Path, None, None]:
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


def run_cmd(msys2_root: PathLike, args: Sequence[PathLike], **kwargs: Any) -> None:
    executable = os.path.join(msys2_root, 'usr', 'bin', 'bash.exe')
    env = clean_environ(kwargs.pop("env", os.environ.copy()))
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    env["MSYS2_PATH_TYPE"] = "minimal"

    def shlex_join(split_command: Sequence[str]) -> str:
        # shlex.join got added in 3.8 while we support 3.6
        return ' '.join(shlex.quote(arg) for arg in split_command)

    check_call([executable, '-lc'] + [shlex_join([str(a) for a in args])], env=env, **kwargs)


def reset_git_repo(path: PathLike):

    def clean():
        assert os.path.exists(path)
        check_call(["git", "clean", "-xfdf"], cwd=path)
        check_call(["git", "reset", "--hard", "HEAD"], cwd=path)

    for i in range(10):
        try:
            clean()
        except subprocess.CalledProcessError:
            # sometimes git clean fails right after the build
            print(f"git clean/reset failed, sleeping for {i} seconds")
            time.sleep(i)
        else:
            break
    else:
        # run it one more time to raise
        clean()


@contextmanager
def fresh_git_repo(url: str, path: PathLike) -> Generator:
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])
    else:
        reset_git_repo(path)
        check_call(["git", "fetch", "origin"], cwd=path)
        check_call(["git", "reset", "--hard", "origin/master"], cwd=path)
    try:
        yield
    finally:
        assert os.path.exists(path)
        reset_git_repo(path)


@contextmanager
def staging_dependencies(
        build_type: BuildType, pkg: Package, msys2_root: PathLike,
        builddir: PathLike) -> Generator[PathLike, None, None]:

    def add_to_repo(repo_root: PathLike, pacman_config: PathLike, repo_name: str,
                    assets: List[GitReleaseAsset]) -> None:
        repo_dir = Path(repo_root) / repo_name
        os.makedirs(repo_dir, exist_ok=True)

        todo = []
        for asset in assets:
            asset_path = os.path.join(repo_dir, get_asset_filename(asset))
            todo.append((asset_path, asset))

        def fetch_item(item: Tuple[str, GitReleaseAsset]) -> Tuple[str, GitReleaseAsset]:
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

        base_args: List[PathLike] = ["repo-add", to_pure_posix_path(repo_db_path)]
        posix_paths: List[PathLike] = [to_pure_posix_path(p) for p in package_paths]
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


def build_package(build_type: BuildType, pkg: Package, msys2_root: PathLike, builddir: PathLike) -> None:
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    to_upload: List[str] = []

    repo = get_repo(build_type)

    with fresh_git_repo(pkg['repo_url'], repo_dir):
        orig_pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
        # Rename it to get a shorter overall build path
        # https://github.com/msys2/msys2-autobuild/issues/71
        pkg_dir = os.path.join(repo_dir, 'B')
        assert not os.path.exists(pkg_dir)
        os.rename(orig_pkg_dir, pkg_dir)

        # Fetch all keys mentioned in the PKGBUILD
        validpgpkeys = to_pure_posix_path(os.path.join(SCRIPT_DIR, 'fetch-validpgpkeys.sh'))
        run_cmd(msys2_root, ['bash', validpgpkeys], cwd=pkg_dir)

        with staging_dependencies(build_type, pkg, msys2_root, builddir) as temp_pacman:
            try:
                env = get_build_environ(build_type)
                # this makes makepkg use our custom pacman script
                env['PACMAN'] = str(to_pure_posix_path(temp_pacman))
                if build_type == Config.MINGW_SRC_BUILD_TYPE:
                    env['MINGW_ARCH'] = Config.MINGW_SRC_ARCH
                    run_cmd(msys2_root, [
                        'makepkg-mingw',
                        '--noconfirm',
                        '--noprogressbar',
                        '--allsource'
                    ], env=env, cwd=pkg_dir)
                elif build_type == Config.MSYS_SRC_BUILD_TYPE:
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
                elif build_type in Config.MSYS_SRC_ARCH:
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
