import sys
import os
import argparse
from os import environ
from github import Github
from pathlib import Path, PurePosixPath
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

# After which overall time it should stop building (in seconds)
HARD_TIMEOUT = 5.7 * 60 * 60
# After which we shouldn't start a new build
SOFT_TIMEOUT = HARD_TIMEOUT / 2

# Packages that take too long to build, and should be handled manually
SKIP = [
    'mingw-w64-clang',
    'mingw-w64-arm-none-eabi-gcc',
    'mingw-w64-gcc',
]


REPO = "msys2/msys2-autobuild"
MSYS2_REPOS = ["mingw32", "mingw64", "msys"]


def timeoutgen(timeout):
    end = time.time() + timeout

    def new():
        return max(end - time.time(), 0)
    return new


get_hard_timeout = timeoutgen(HARD_TIMEOUT)
get_soft_timeout = timeoutgen(SOFT_TIMEOUT)


def run_cmd(msys2_root, args, **kwargs):
    executable = os.path.join(msys2_root, 'usr', 'bin', 'bash.exe')
    env = kwargs.pop("env", os.environ.copy())
    env["CHERE_INVOKING"] = "1"
    env["MSYSTEM"] = "MSYS"
    env["MSYS2_PATH_TYPE"] = "minimal"
    check_call([executable, '-lc'] + [shlex.join([str(a) for a in args])], env=env, **kwargs)


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
        try:
            check_call(["git", "clean", "-xfdf"], cwd=path)
        except subprocess.CalledProcessError:
            # sometimes it fails right after the build has failed
            # not sure why
            pass
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


def download_asset(asset, target_path: str, timeout=15) -> None:
    with requests.get(asset.browser_download_url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(target_path, 'wb') as h:
            for chunk in r.iter_content(4096):
                h.write(chunk)


def upload_asset(repo_type: str, path: os.PathLike, replace: bool = True) -> None:
    # type_: msys/mingw/failed
    if not environ.get("CI"):
        print("WARNING: upload skipped, not running in CI")
        return
    path = Path(path)
    gh = Github(*get_credentials())
    repo = gh.get_repo(REPO)
    release_name = "staging-" + repo_type
    release = repo.get_release(release_name)

    basename = os.path.basename(str(path))
    asset_name = get_gh_asset_name(basename)
    asset_label = basename

    if replace:
        for asset in get_release_assets(repo, release_name):
            if asset_name == asset.name:
                asset.delete_asset()

    release.upload_asset(str(path), label=asset_label, name=asset_name)
    print(f"Uploaded {asset_name} as {asset_label}")


def get_python_path(msys2_root, msys2_path):
    return Path(os.path.normpath(msys2_root + msys2_path))


def to_pure_posix_path(path):
    return PurePosixPath("/" + str(path).replace(":", "", 1).replace("\\", "/"))


@contextmanager
def backup_pacman_conf(msys2_root):
    conf = get_python_path(msys2_root, "/etc/pacman.conf")
    backup = get_python_path(msys2_root, "/etc/pacman.conf.backup")
    shutil.copyfile(conf, backup)
    try:
        yield
    finally:
        os.replace(backup, conf)


@contextmanager
def auto_key_retrieve(msys2_root):
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
keyserver keyserver.ubuntu.com
keyserver-options auto-key-retrieve
""")
        yield
    finally:
        if backup is not None:
            os.replace(backup, conf)


@contextmanager
def staging_dependencies(pkg, msys2_root, builddir):
    gh = Github(*get_credentials())
    repo = gh.get_repo(REPO)

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
                text.replace("#RemoteFileSigLevel = Required",
                             "RemoteFileSigLevel = Never")
                with open(conf, "w", encoding="utf-8") as h2:
                    h2.write(f"""[{repo_name}]
Server={uri}
SigLevel=Never
""")
                    h2.write(text)

        run_cmd(msys2_root, ["repo-add", to_pure_posix_path(repo_db_path),
                             to_pure_posix_path(package_path)], cwd=repo_dir)

    repo_root = os.path.join(builddir, "_REPO")
    try:
        shutil.rmtree(repo_root, ignore_errors=True)
        os.makedirs(repo_root, exist_ok=True)
        with backup_pacman_conf(msys2_root):
            to_add = []
            for repo_type, deps in pkg['ext-depends'].items():
                for name, dep in deps.items():
                    pattern = f"{name}-{dep['version']}-*.pkg.*"
                    for asset in get_release_assets(repo, "staging-" + repo_type):
                        if fnmatch.fnmatch(get_asset_filename(asset), pattern):
                            to_add.append((repo_type, asset))
                            break
                    else:
                        raise MissingDependencyError(f"asset for {pattern} not found")

            for repo_type, asset in to_add:
                add_to_repo(repo_root, repo_type, asset)

            # in case they are already installed we need to upgrade
            run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suy"])
            yield
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
        # downgrade again
        run_cmd(msys2_root, ["pacman", "--noconfirm", "-Suuy"])


def build_package(pkg, msys2_root, builddir):
    assert os.path.isabs(builddir)
    assert os.path.isabs(msys2_root)
    os.makedirs(builddir, exist_ok=True)

    repo_name = {"MINGW-packages": "M", "MSYS2-packages": "S"}.get(pkg['repo'], pkg['repo'])
    repo_dir = os.path.join(builddir, repo_name)
    is_msys = pkg['repo'].startswith('MSYS2')

    with staging_dependencies(pkg, msys2_root, builddir), \
            auto_key_retrieve(msys2_root), \
            fresh_git_repo(pkg['repo_url'], repo_dir):
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
        makepkg = 'makepkg' if is_msys else 'makepkg-mingw'

        try:
            run_cmd(msys2_root, [
                makepkg,
                '--noconfirm',
                '--noprogressbar',
                '--nocheck',
                '--syncdeps',
                '--rmdeps',
                '--cleanbuild'
            ], cwd=pkg_dir, timeout=get_hard_timeout())

            env = environ.copy()
            if not is_msys:
                env['MINGW_INSTALLS'] = 'mingw64'
            run_cmd(msys2_root, [
                makepkg,
                '--noconfirm',
                '--noprogressbar',
                '--allsource'
            ], env=env, cwd=pkg_dir, timeout=get_hard_timeout())
        except subprocess.TimeoutExpired as e:
            raise BuildTimeoutError(e)
        except subprocess.CalledProcessError as e:

            for repo, items in pkg['packages'].items():
                for item in items:
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
                    if is_msys:
                        repo = "msys"
                    elif entry.startswith("mingw-w64-i686-"):
                        repo = "mingw32"
                    else:
                        repo = "mingw64"

                    upload_asset(repo, path)


def run_build(args):
    builddir = os.path.abspath(args.builddir)
    msys2_root = os.path.abspath(args.msys2_root)

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
        pkg = todo[0]
        key = pkg['repo'] + pkg['name']
        if key in done:
            raise SystemExit("ERROR: building package again in the same run", pkg)
        done.add(key)

        if get_soft_timeout() == 0:
            print("timeout reached")
            break

        try:
            with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }..."):
                build_package(pkg, msys2_root, builddir)
        except MissingDependencyError:
            with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }: failed -> missing deps"):
                pass
            continue
        except BuildTimeoutError:
            with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }: build time limit reached"):
                pass
            break
        except BuildError:
            with gha_group(f"[{ pkg['repo'] }] { pkg['name'] }: failed"):
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
        for repo, names in pkg['packages'].items():
            for name in names:
                dep_mapping[name] = pkg

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        ver_depends = {}
        for repo, deps in pkg['depends'].items():
            for dep in deps:
                ver_depends.setdefault(repo, {})[dep] = dep_mapping[dep]
        pkg['ext-depends'] = ver_depends

    return pkgs


def get_gh_asset_name(basename):
    # GitHub will throw out charaters like '~' or '='. It also doesn't like
    # when there is no file extension and will try to add one
    return sha256(str(basename).encode("utf-8")).hexdigest() + ".bin"


def get_asset_filename(asset):
    if not asset.label:
        return asset.name
    else:
        assert get_gh_asset_name(asset.label) == asset.name
        return asset.label


def get_release_assets(repo, release_name):
    assets = []
    for asset in repo.get_release(release_name).get_assets():
        uploader = asset.uploader
        if uploader.type != "Bot" or uploader.login != "github-actions[bot]":
            raise SystemExit(f"ERROR: Asset '{get_asset_filename(asset)}' not uploaded "
                             f"by GHA but '{uploader.login}'. Aborting.")
        assets.append(asset)
    return assets


def get_packages_to_build():
    gh = Github(*get_credentials())

    repo = gh.get_repo(REPO)
    assets = []
    for repo_type in MSYS2_REPOS:
        assets.extend([
            get_asset_filename(a) for a in get_release_assets(repo, 'staging-' + repo_type)])
    assets_failed = [
        get_asset_filename(a) for a in get_release_assets(repo, 'staging-failed')]

    def pkg_is_done(pkg):
        for repo, items in pkg['packages'].items():
            for item in items:
                if not fnmatch.filter(assets, f"{item}-{pkg['version']}-*.pkg.tar.*"):
                    return False
        return True

    def pkg_has_failed(pkg):
        for repo, items in pkg['packages'].items():
            for item in items:
                if f"{item}-{pkg['version']}.failed" in assets_failed:
                    return True
        return False

    def pkg_is_skipped(pkg):
        return pkg['name'] in SKIP

    todo = []
    done = []
    skipped = []
    for pkg in get_buildqueue():
        if pkg_is_done(pkg):
            done.append(pkg)
        elif pkg_has_failed(pkg):
            skipped.append((pkg, "failed"))
        elif pkg_is_skipped(pkg):
            skipped.append((pkg, "skipped"))
        else:
            for repo, deps in pkg['ext-depends'].items():
                for dep in deps.values():
                    if pkg_has_failed(dep) or pkg_is_skipped(dep):
                        skipped.append((pkg, "requires: " + dep['name']))
                        break
                else:
                    todo.append(pkg)

    return done, skipped, todo


def show_build(args):
    done, skipped, todo = get_packages_to_build()

    with gha_group(f"TODO ({len(todo)})"):
        print(tabulate([(p["name"], p["version"]) for p in todo],
                       headers=["Package", "Version"]))

    with gha_group(f"SKIPPED ({len(skipped)})"):
        print(tabulate([(p["name"], p["version"], r) for (p, r) in skipped],
                       headers=["Package", "Version", "Reason"]))

    with gha_group(f"DONE ({len(done)})"):
        print(tabulate([(p["name"], p["version"]) for p in done],
                       headers=["Package", "Version"]))

    if args.fail_on_idle and not todo:
        raise SystemExit("Nothing to build")


def show_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo(REPO)

    for repo_type in MSYS2_REPOS:
        assets = get_release_assets(repo, 'staging-' + repo_type)

        print(tabulate(
            [[
                get_asset_filename(asset),
                asset.size,
                asset.created_at,
                asset.updated_at,
            ] for asset in assets],
            headers=["name", "size", "created", "updated"]
        ))


def get_repo_subdir(repo_type: str, asset):
    entry = get_asset_filename(asset)
    t = Path(repo_type)
    if repo_type == "msys":
        if fnmatch.fnmatch(entry, '*.pkg.tar.*'):
            return t / "x86_64"
        elif fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        else:
            raise Exception("unknown file type")
    else:
        if fnmatch.fnmatch(entry, '*.pkg.tar.*'):
            if repo_type == "mingw32":
                return t / "i686"
            elif repo_type == "mingw64":
                return t / "x86_64"
        elif fnmatch.fnmatch(entry, '*.src.tar.*'):
            return t / "sources"
        else:
            raise Exception("unknown file type")


def fetch_assets(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo(REPO)

    todo = []
    skipped = []
    for repo_type in MSYS2_REPOS:
        p = Path(args.targetdir)
        assets = get_release_assets(repo, 'staging-' + repo_type)
        for asset in assets:
            asset_dir = p / get_repo_subdir(repo_type, asset)
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path = asset_dir / get_asset_filename(asset)
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
            print(f"[{i + 1}/{len(todo)}] {get_asset_filename(item[0])}")

    print("done")


def trigger_gha_build(args):
    gh = Github(*get_credentials())
    repo = gh.get_repo(REPO)
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
        for repo, items in pkg['packages'].items():
            for item in items:
                patterns.append(f"{item}-{pkg['version']}*")

    print("Fetching assets...")
    assets = {}
    repo = gh.get_repo(REPO)
    for repo_type in MSYS2_REPOS + ['failed']:
        for asset in get_release_assets(repo, 'staging-' + repo_type):
            assets.setdefault(get_asset_filename(asset), []).append(asset)

    for pattern in patterns:
        for key in fnmatch.filter(assets.keys(), pattern):
            del assets[key]

    for items in assets.values():
        for asset in items:
            print(f"Deleting {get_asset_filename(asset)}...")
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
