import os

from msys2_autobuild.build import get_packager


def test_get_packager():
    os.environ["GITHUB_RUN_ID"] = "12345"
    os.environ["JOB_CHECK_RUN_ID"] = "67890"
    packager = get_packager("ucrt64")
    assert "msys2/msys2-autobuild/actions/runs/12345/job/67890" in packager
