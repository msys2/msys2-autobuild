# type: ignore

import os
import stat
import tempfile
from pathlib import Path

from msys2_autobuild.utils import parse_optional_deps
from msys2_autobuild.queue import parse_buildqueue, get_cycles
from msys2_autobuild.build import make_tree_writable, remove_junctions


def test_make_tree_writable():
    with tempfile.TemporaryDirectory() as tempdir:
        nested_dir = Path(tempdir) / "nested"
        nested_junction = nested_dir / "junction"
        nested_dir.mkdir()
        file_path = nested_dir / "test_file.txt"
        file_path.write_text("content")

        # Create a junction loop if possible, to make sure we ignore it
        if hasattr(os.path, 'isjunction') and os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(nested_dir), str(nested_junction))
        else:
            nested_junction.mkdir()

        # Remove permissions
        for p in [tempdir, nested_dir, file_path, nested_junction]:
            os.chmod(p, os.stat(p).st_mode & ~stat.S_IWRITE & ~stat.S_IREAD)

        make_tree_writable(tempdir)

        assert os.access(tempdir, os.W_OK) and os.access(tempdir, os.R_OK)
        assert os.access(nested_dir, os.W_OK) and os.access(nested_dir, os.R_OK)
        assert os.access(file_path, os.W_OK) and os.access(file_path, os.R_OK)
        assert os.access(nested_junction, os.W_OK) and os.access(nested_junction, os.R_OK)


def test_remove_junctions():
    with tempfile.TemporaryDirectory() as tempdir:
        nested_dir = Path(tempdir) / "nested"
        nested_junction = nested_dir / "junction"
        nested_dir.mkdir()

        # Create a junction loop if possible, to make sure we ignore it
        if hasattr(os.path, 'isjunction') and os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(nested_dir), str(nested_junction))
            assert nested_junction.exists()
            assert os.path.isjunction(nested_junction)

        remove_junctions(tempdir)
        assert not nested_junction.exists()


def test_parse_optional_deps():
    assert parse_optional_deps("a:b,c:d,a:x") == {'a': ['b', 'x'], 'c': ['d']}


def test_get_cycles():
    buildqueue = """
[
  {
    "name": "c-ares",
    "version": "1.34.2-1",
    "version_repo": "1.33.1-1",
    "repo_url": "https://github.com/msys2/MSYS2-packages",
    "repo_path": "c-ares",
    "source": true,
    "builds": {
      "msys": {
        "packages": [
          "libcares",
          "libcares-devel"
        ],
        "depends": {
          "msys": [
            "libnghttp2",
            "libuv"
          ]
        },
        "new": false
      }
    }
  },
  {
    "name": "nghttp2",
    "version": "1.64.0-1",
    "version_repo": "1.63.0-1",
    "repo_url": "https://github.com/msys2/MSYS2-packages",
    "repo_path": "nghttp2",
    "source": true,
    "builds": {
      "msys": {
        "packages": [
          "libnghttp2",
          "libnghttp2-devel",
          "nghttp2"
        ],
        "depends": {
          "msys": [
            "libcares",
            "libcares-devel"
          ]
        },
        "new": false
      }
    }
  },
  {
    "name": "libuv",
    "version": "1.49.2-1",
    "version_repo": "1.49.1-1",
    "repo_url": "https://github.com/msys2/MSYS2-packages",
    "repo_path": "libuv",
    "source": true,
    "builds": {
      "msys": {
        "packages": [
          "libuv",
          "libuv-devel"
        ],
        "depends": {
          "msys": [
            "libnghttp2"
          ]
        },
        "new": false
      }
    }
  }
]"""

    pkgs = parse_buildqueue(buildqueue)
    cycles = get_cycles(pkgs)
    assert len(cycles) == 3
    assert (pkgs[0], pkgs[2]) in cycles
    assert (pkgs[0], pkgs[1]) in cycles
    assert (pkgs[2], pkgs[1]) in cycles
