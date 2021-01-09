import sys
import os
import argparse
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
from hashlib import sha256
from typing import Generator, Union, AnyStr, List, Any, Dict, Tuple, Set, Sequence, Collection

_PathLike = Union[os.PathLike, AnyStr]


class _Package(dict):

    def __repr__(self):
        return "Package(%r)" % self["name"]

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


# After which we shouldn't start a new build
SOFT_TIMEOUT = 60 * 60 * 3

# Packages that take too long to build, and should be handled manually
SKIP: List[str] = [
    # 'mingw-w64-clang',
    # 'mingw-w64-arm-none-eabi-gcc',
    # 'mingw-w64-gcc',
    'mingw-w64-gcc-git',
    'mingw-w64-firebird-git',
    'mingw-w64-qt5-static',
    'mingw-w64-blender',
]


# FIXME: Packages that should be ignored if they depend on other things
# in the queue. Ideally this list should be empty..
IGNORE_RDEP_PACKAGES: List[str] = [
    "mingw-w64-vrpn",
    "mingw-w64-cocos2d-x",
    "mingw-w64-mlpack",
    "mingw-w64-qemu",
    "mingw-w64-ghc",
    "mingw-w64-python-notebook",
    "mingw-w64-python-pywin32",
    "mingw-w64-usbmuxd",
    "mingw-w64-ldns",
    "mingw-w64-npm",
    "mingw-w64-yarn",
    "mingw-w64-bower",
    "mingw-w64-nodejs",
    "mingw-w64-cross-conemu-git",
    "mingw-w64-blender",
    "mingw-w64-godot-cpp",
]


REPO = "msys2/msys2-autobuild"
WORKFLOW = "build"
RUN_ID = os.getenv('GITHUB_RUN_ID','oh no')

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


def upload_asset(release: GitRelease, path: _PathLike, replace: bool = False) -> None:
    # type_: msys/mingw/failed
    if not environ.get("CI"):
        print("WARNING: upload skipped, not running in CI")
        return
    path = Path(path)

    basename = os.path.basename(str(path))
    asset_name = get_gh_asset_name(basename)
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

    release.upload_asset(str(path), label=asset_label, name=asset_name)
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


def build_type_to_dep_type(build_type):
    if build_type == "mingw-src":
        dep_type = "mingw64"
    elif build_type == "msys-src":
        dep_type = "msys"
    else:
        dep_type = build_type
    return dep_type


@contextmanager
def staging_dependencies(
        build_type: str, pkg: _Package, msys2_root: _PathLike,
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
            dep_type = build_type_to_dep_type(build_type)
            for name, dep in pkg['ext-depends'].get(dep_type, {}).items():
                pattern = f"{name}-{dep['version']}-*.pkg.*"
                repo_type = "msys" if dep['repo'].startswith('MSYS2') else "mingw"
                for asset in get_cached_assets(repo, "staging-" + repo_type):
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

            patterns = []
            if build_type in ["mingw-src", "msys-src"]:
                patterns.append(f"{pkg['name']}-{pkg['version']}.src.tar.*")
            elif build_type in ["mingw32", "mingw64", "msys"]:
                for item in pkg['packages'].get(build_type, []):
                    patterns.append(f"{item}-{pkg['version']}-*.pkg.tar.*")
            else:
                assert 0

            entries = os.listdir(pkg_dir)
            for pattern in patterns:
                found = fnmatch.filter(entries, pattern)
                if not found:
                    raise BuildError(f"{pattern} not found, likely wrong version built")
                to_upload.extend([os.path.join(pkg_dir, e) for e in found])

        except (subprocess.CalledProcessError, BuildError) as e:
            failed_entries = []
            if build_type in ["mingw-src", "msys-src"]:
                failed_entries.append(f"{pkg['name']}-{pkg['version']}.failed")
            elif build_type in ["mingw32", "mingw64", "msys"]:
                for item in pkg['packages'].get(build_type, []):
                    failed_entries.append(f"{item}-{pkg['version']}.failed")
            else:
                assert 0

            release = repo.get_release("staging-failed")
            for entry in failed_entries:
                with tempfile.TemporaryDirectory() as tempdir:
                    failed_path = os.path.join(tempdir, entry)
                    with open(failed_path, 'w') as h:
                        # github doesn't allow empty assets
                        h.write(f"https://github.com/{REPO}/runs/{RUN_ID}")
                    upload_asset(release, failed_path)

            raise BuildError(e)
        else:
            release = repo.get_release(
                "staging-msys"if build_type.startswith("msys") else "staging-mingw")
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

    done = set()
    while True:
        todo = get_packages_to_build()[2]
        if not todo:
            break
        pkg, build_type = todo[0]
        key = pkg['repo'] + build_type + pkg['name'] + pkg['version']
        if key in done:
            raise SystemExit("ERROR: building package again in the same run", pkg)
        done.add(key)

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


def get_buildqueue() -> List[_Package]:
    pkgs = []
    r = requests.get("https://packages.msys2.org/api/buildqueue?include_vcs=true")
    r.raise_for_status()
    dep_mapping = {}
    for received in r.json():
        pkg = _Package(received)
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)
        for repo, names in pkg['packages'].items():
            for name in names:
                dep_mapping[name] = pkg

    # We need to pull in all packages of that particular build because they can
    # depend on each other with a fixed version
    for pkg in pkgs:
        for repo, deps in pkg['depends'].items():
            all_deps = set(deps)
            for dep in deps:
                dep_pkg = dep_mapping[dep]
                all_deps.update(dep_pkg['packages'][repo])
            pkg['depends'][repo] = sorted(all_deps)

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        ver_depends: Dict[str, Dict[str, _Package]] = {}
        for repo, deps in pkg['depends'].items():
            for dep in deps:
                ver_depends.setdefault(repo, {})[dep] = dep_mapping[dep]
        pkg['ext-depends'] = ver_depends

    # reverse dependencies
    for pkg in pkgs:
        r_depends: Dict[str, Set[_Package]] = {}
        for pkg2 in pkgs:
            for repo, deps in pkg2['ext-depends'].items():
                if pkg in deps.values():
                    r_depends.setdefault(repo, set()).add(pkg2)
        pkg['ext-rdepends'] = r_depends

    return pkgs


def get_gh_asset_name(basename: _PathLike) -> str:
    # GitHub will throw out charaters like '~' or '='. It also doesn't like
    # when there is no file extension and will try to add one
    return sha256(str(basename).encode("utf-8")).hexdigest() + ".bin"


def get_asset_filename(asset: GitReleaseAsset) -> str:
    if not asset.label:
        return asset.name
    else:
        assert get_gh_asset_name(asset.label) == asset.name
        return asset.label


def get_release_assets(release: GitRelease, include_incomplete=False) -> List[GitReleaseAsset]:
    assets = []
    for asset in release.get_assets():
        # skip in case not fully uploaded yet (or uploading failed)
        if not asset_is_complete(asset) and not include_incomplete:
            continue
        uploader = asset.uploader
        if uploader.type != "Bot" or uploader.login != "github-actions[bot]":
            raise SystemExit(f"ERROR: Asset '{get_asset_filename(asset)}' not uploaded "
                             f"by GHA but '{uploader.login}'. Aborting.")
        assets.append(asset)
    return assets


def get_packages_to_build() -> Tuple[
        List[Tuple[_Package, str]], List[Tuple[_Package, str, str]],
        List[Tuple[_Package, str]]]:
    repo = get_repo(optional_credentials=True)
    assets = []
    for name in ["msys", "mingw"]:
        release = repo.get_release('staging-' + name)
        assets.extend([
            get_asset_filename(a) for a in get_release_assets(release)])
    release = repo.get_release('staging-failed')
    assets_failed = [
        get_asset_filename(a) for a in get_release_assets(release)]

    def pkg_is_done(build_type: str, pkg: _Package) -> bool:
        if build_type in ["mingw-src", "msys-src"]:
            if not fnmatch.filter(assets, f"{pkg['name']}-{pkg['version']}.src.tar.*"):
                return False
        else:
            for item in pkg['packages'].get(build_type, []):
                if not fnmatch.filter(assets, f"{item}-{pkg['version']}-*.pkg.tar.*"):
                    return False
        return True

    def pkg_has_failed(build_type: str, pkg: _Package) -> bool:
        if build_type in ["mingw-src", "msys-src"]:
            if f"{pkg['name']}-{pkg['version']}.failed" in assets_failed:
                return True
        else:
            for item in pkg['packages'].get(build_type, []):
                if f"{item}-{pkg['version']}.failed" in assets_failed:
                    return True
        return False

    def pkg_is_skipped(build_type: str, pkg: _Package) -> bool:
        # XXX: If none is build, skip the src build
        if build_type in ["mingw-src", "msys-src"]:
            if not any(pkg_is_done(bt, pkg) for bt in pkg["packages"].keys()):
                return True

        for other, other_type, msg in skipped:
            if build_type == other_type and pkg is other:
                return True

        return pkg['name'] in SKIP

    def pkg_needs_build(build_type: str, pkg: _Package) -> bool:
        if build_type in pkg["packages"]:
            return True
        if build_type == "mingw-src" and \
                any(k.startswith("mingw") for k in pkg["packages"].keys()):
            return True
        if build_type == "msys-src" and "msys" in pkg["packages"]:
            return True
        return False

    todo = []
    done = []
    skipped = []
    for pkg in get_buildqueue():
        for build_type in ["msys", "mingw32", "mingw64", "mingw-src", "msys-src"]:
            if not pkg_needs_build(build_type, pkg):
                continue
            if pkg_is_done(build_type, pkg):
                done.append((pkg, build_type))
            elif pkg_has_failed(build_type, pkg):
                skipped.append((pkg, build_type, "failed"))
            elif pkg_is_skipped(build_type, pkg):
                skipped.append((pkg, build_type, "skipped"))
            else:
                dep_type = build_type_to_dep_type(build_type)
                for dep in pkg['ext-depends'].get(dep_type, {}).values():
                    if pkg_has_failed(dep_type, dep) or pkg_is_skipped(dep_type, dep):
                        skipped.append((pkg, build_type, "requires: " + dep['name']))
                        break
                else:
                    todo.append((pkg, build_type))

    return done, skipped, todo


def get_workflow():
    repo = get_repo()
    for workflow in repo.get_workflows():
        if workflow.name == WORKFLOW:
            return workflow
    else:
        raise Exception("workflow not found:", WORKFLOW)


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

    done, skipped, todo = get_packages_to_build()
    if not todo:
        raise SystemExit("Nothing to build")


def show_build(args: Any) -> None:
    done, skipped, todo = get_packages_to_build()

    with gha_group(f"TODO ({len(todo)})"):
        print(tabulate([(p["name"], bt, p["version"]) for (p, bt) in todo],
                       headers=["Package", "Build", "Version"]))

    with gha_group(f"SKIPPED ({len(skipped)})"):
        print(tabulate([(p["name"], bt, p["version"], r) for (p, bt, r) in skipped],
                       headers=["Package", "Build", "Version", "Reason"]))

    with gha_group(f"DONE ({len(done)})"):
        print(tabulate([(p["name"], bt, p["version"]) for (p, bt) in done],
                       headers=["Package", "Build", "Version"]))


def show_assets(args: Any) -> None:
    repo = get_repo()

    rows = []
    for name in ["msys", "mingw"]:
        release = repo.get_release('staging-' + name)
        assets = get_release_assets(release)
        rows += [[
            name,
            get_asset_filename(asset),
            asset.size,
            asset.created_at,
            asset.updated_at,
        ] for asset in assets]

    print(tabulate(rows, headers=["repo", "name", "size", "created", "updated"]))


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


def fetch_assets(args: Any) -> None:
    repo = get_repo(optional_credentials=True)

    pkgs = get_buildqueue()

    todo = []
    done = []
    all_blocked = {}
    for name, repo_name in [("msys", "MSYS2-packages"), ("mingw", "MINGW-packages")]:
        p = Path(args.targetdir)
        release = repo.get_release('staging-' + name)
        release_assets = get_release_assets(release)
        repo_pkgs = [p for p in pkgs if p["repo"] == repo_name]
        finished_assets, blocked = get_finished_assets(
            repo_pkgs, release_assets, args.fetch_all)
        all_blocked.update(blocked)

        for pkg, assets in finished_assets.items():
            for asset in assets:
                asset_dir = p / get_repo_subdir(name, asset)
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


def trigger_gha_build(args: Any) -> None:
    repo = get_repo()
    workflow = get_workflow()
    if workflow.create_dispatch(repo.default_branch):
        print("Build triggered")
    else:
        raise Exception("trigger failed")


def get_assets_to_delete(repo: Repository) -> List[GitReleaseAsset]:
    print("Fetching packages to build...")
    patterns = []
    for pkg in get_buildqueue():
        patterns.append(f"{pkg['name']}-{pkg['version']}.src.tar.*")
        patterns.append(f"{pkg['name']}-{pkg['version']}.failed")
        for items in pkg['packages'].values():
            for item in items:
                patterns.append(f"{item}-{pkg['version']}-*.pkg.tar.*")
                patterns.append(f"{item}-{pkg['version']}.failed")

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


def get_finished_assets(pkgs: Collection[_Package],
                        assets: Sequence[GitReleaseAsset],
                        ignore_blocked: bool) -> Tuple[
        Dict[_Package, List[GitReleaseAsset]], Dict[_Package, str]]:
    """Returns assets for packages where all package results are available"""

    assets_mapping: Dict[str, List[GitReleaseAsset]] = {}
    for asset in assets:
        assets_mapping.setdefault(get_asset_filename(asset), []).append(asset)

    finished = {}
    for pkg in pkgs:
        # Only returns assets for packages where everything has been
        # built already
        patterns = []
        patterns.append(f"{pkg['name']}-{pkg['version']}.src.tar.*")
        for repo, items in pkg['packages'].items():
            for item in items:
                patterns.append(f"{item}-{pkg['version']}-*.pkg.tar.*")

        finished_maybe = []
        for pattern in patterns:
            matches = fnmatch.filter(assets_mapping.keys(), pattern)
            if matches:
                found = assets_mapping[matches[0]]
                finished_maybe.extend(found)

        if len(finished_maybe) == len(patterns):
            finished[pkg] = finished_maybe

    blocked = {}

    if not ignore_blocked:
        for pkg in finished:
            blocked_reason = set()

            # skip packages where not all dependencies have been built
            for repo, deps in pkg["ext-depends"].items():
                for dep in deps.values():
                    if dep in pkgs and dep not in finished:
                        blocked_reason.add(dep)

            # skip packages where not all reverse dependencies have been built
            for repo, deps in pkg["ext-rdepends"].items():
                for dep in deps:
                    if dep["name"] in IGNORE_RDEP_PACKAGES:
                        continue
                    if dep in pkgs and dep not in finished:
                        blocked_reason.add(dep)

            if blocked_reason:
                blocked[pkg] = "waiting on %r" % blocked_reason

        for pkg in blocked:
            finished.pop(pkg, None)

    return finished, blocked


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
        "show-assets", help="Show all staging packages", allow_abbrev=False)
    sub.set_defaults(func=show_assets)

    sub = subparser.add_parser(
        "fetch-assets", help="Download all staging packages", allow_abbrev=False)
    sub.add_argument("targetdir")
    sub.add_argument(
        "--verbose", action="store_true", help="Show why things are blocked")
    sub.add_argument(
        "--pretend", action="store_true", help="Don't actually download, just show what would be done")
    sub.add_argument(
        "--fetch-all", action="store_true", help="Fetch all packages, even blocked ones")
    sub.set_defaults(func=fetch_assets)

    sub = subparser.add_parser("trigger", help="Trigger a GHA build", allow_abbrev=False)
    sub.set_defaults(func=trigger_gha_build)

    sub = subparser.add_parser("clean-assets", help="Clean up GHA assets", allow_abbrev=False)
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be deleted")
    sub.set_defaults(func=clean_gha_assets)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)
