# type: ignore

from msys2_autobuild.utils import parse_optional_deps


def test_parse_optional_deps():
    assert parse_optional_deps("a:b,c:d,a:x") == {'a': ['b', 'x'], 'c': ['d']}
