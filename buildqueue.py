from os import environ
from github import Github
from urllib.request import urlopen
from json import loads
from pathlib import Path
from subprocess import check_call
from sys import stdout
import fnmatch
import pytest

ROOT = Path(__file__).resolve().parent

def test_file(pkg):
    pkg['repo'] = pkg['repo_url'].split('/')[-1]

    isMSYS = pkg['repo'][0:5] == 'MSYS2'

    print('\n::group::[%s] %s...' % (pkg['repo'], pkg['repo_path']))
    stdout.flush()

    def run_cmd(args):
        check_call(['bash', '-c'] + [' '.join(args)], cwd=str(ROOT.parent/pkg['repo']/pkg['repo_path']))

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
        run_cmd(['mv', '*.pkg.tar.*', '*.src.tar.gz', '../../msys2-devtools/assets/'])
        print('::endgroup::')
        stdout.flush()
    except:
        print('::endgroup::')
        stdout.flush()
        pytest.fail()

def pytest_generate_tests(metafunc):
    gh = Github(environ["GITHUB_TOKEN"])
    #gh = Github(environ["GITHUB_USER"], environ["GITHUB_PASS"])

    assets = [
        asset.name
        for asset in gh.get_repo('msys2/msys2-devtools').get_release('staging').get_assets()
    ]

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

    print("\nSKIPPED packages; already in 'staging':")
    for pkg in done:
        print(" -", pkg['repo_path'])

    # Packages that take too long to build, and should be handled manually
    skip = ['mingw-w64-clang']

    metafunc.parametrize("pkg", [
        pkg for pkg in tasks if pkg['repo_path'] not in skip
    ])
