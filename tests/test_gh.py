from unittest.mock import MagicMock

import pytest
from github import GitReleaseAsset

from msys2_autobuild.gh import encode_filename, decode_filename, get_upload_safe_name, \
    get_asset_filename


def test_encode_decode_filename():
    for filename in ["test.txt", "file with spaces.txt", "weird~name=with#chars!.bin", ""]:
        encoded = encode_filename(filename)
        decoded = decode_filename(encoded)
        assert decoded == filename


def test_get_gh_asset_name():
    assert get_upload_safe_name("test.txt") == encode_filename("test.txt") + ".bin"
    assert get_upload_safe_name("test.txt", text=True) == encode_filename("test.txt") + ".txt"

    with pytest.raises(ValueError):
        get_upload_safe_name("")


def test_get_asset_filename():
    asset = MagicMock(spec=GitReleaseAsset.GitReleaseAsset)
    asset.name = get_upload_safe_name("test.txt")
    asset.label = "test.txt"
    assert get_asset_filename(asset) == "test.txt"
