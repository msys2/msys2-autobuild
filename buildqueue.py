from urllib.request import urlopen
from json import loads
from pathlib import Path
from subprocess import check_call
from sys import stdout
import pytest

PKGEXT='.pkg.tar.zst'
SRCEXT='.src.tar.gz'

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
        run_cmd(['mv', '*%s' % PKGEXT, '*%s' % SRCEXT, '../../msys2-devtools/assets/'])
        print('::endgroup::')
        stdout.flush()
    except:
        print('::endgroup::')
        stdout.flush()
        pytest.fail()

def pytest_generate_tests(metafunc):
    metafunc.parametrize("pkg", [
        pkg for pkg in loads(urlopen("https://packages.msys2.org/api/buildqueue").read())
        if pkg['repo_path'] not in ('mingw-w64-clang')
    ])
