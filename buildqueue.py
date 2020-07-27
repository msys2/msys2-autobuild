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
from sys import stdout
import fnmatch
import pytest
from tabulate import tabulate


# Packages that take too long to build, and should be handled manually
SKIP = [
    'mingw-w64-clang',
]


def ensure_git_repo(url, path):
    if not os.path.exists(path):
        check_call(["git", "clone", url, path])


def test_file(pkg):
    builddir = environ["MSYS2_BUILDIR"]
    assert os.path.isabs(builddir)
    os.makedirs(builddir, exist_ok=True)
    assetdir_msys = os.path.join(builddir, "assets", "msys")
    os.makedirs(assetdir_msys, exist_ok=True)
    assetdir_mingw = os.path.join(builddir, "assets", "mingw")
    os.makedirs(assetdir_mingw, exist_ok=True)

    pkg['repo'] = pkg['repo_url'].split('/')[-1]

    isMSYS = pkg['repo'][0:5] == 'MSYS2'

    print('\n::group::[%s] %s...' % (pkg['repo'], pkg['repo_path']))
    stdout.flush()

    def run_cmd(args):
        repo_dir = os.path.join(builddir, pkg['repo'])
        ensure_git_repo(pkg['repo_url'], repo_dir)
        pkg_dir = os.path.join(repo_dir, pkg['repo_path'])
        check_call(['bash', '-c'] + [' '.join(args)], cwd=pkg_dir)

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

        for entry in os.listdir(pkg_dir):
            if fnmatch(entry, '*.pkg.tar.*') or fnmatch(entry, '*.src.tar.*'):
                shutil.move(entry, assetdir_msys if isMSYS else assetdir_mingw)

        print('::endgroup::')
        stdout.flush()
    except:
        print('::endgroup::')
        stdout.flush()
        pytest.fail()


def get_packages_to_build():
    gh = Github(*get_credentials())

    repo = gh.get_repo('msys2/msys2-devtools')
    assets = []
    for name in ["msys", "mingw"]:
        assets.extend(repo.get_release('staging-' + name).get_assets())

    tasks = []
    done = []

    for pkg in loads(urlopen("https://packages.msys2.org/api/buildqueue").read()):
        isMSYS = pkg['repo_url'].split('/')[-1][0:5] == 'MSYS2'
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


def pytest_generate_tests(metafunc):
    metafunc.parametrize("pkg", get_packages_to_build()[2])


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


def run_build(args):
    environ["MSYS2_BUILDIR"] = os.path.abspath(args.builddir)
    return pytest.main(["-v", "-s", "-ra", "--timeout=18000", __file__])


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

    sub = subparser.add_parser("build")
    sub.add_argument("builddir")
    sub.set_defaults(func=run_build)

    sub = subparser.add_parser("show")
    sub.set_defaults(func=show_build)

    sub = subparser.add_parser("show-assets")
    sub.set_defaults(func=show_assets)

    get_credentials()

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    main(sys.argv)