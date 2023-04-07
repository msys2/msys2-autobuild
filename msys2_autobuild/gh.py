import io
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import requests
from github import Github
from github.GithubException import GithubException, UnknownObjectException
from github.GithubObject import GithubObject
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset
from github.Repository import Repository

from .config import REQUESTS_RETRY, REQUESTS_TIMEOUT, BuildType, Config
from .utils import PathLike, get_requests_session


def get_credentials(write: bool = False) -> Dict[str, Any]:
    if not write and os.environ.get("GITHUB_TOKEN_READONLY", ""):
        return {'login_or_token': os.environ["GITHUB_TOKEN_READONLY"]}
    elif "GITHUB_TOKEN" in os.environ:
        return {'login_or_token': os.environ["GITHUB_TOKEN"]}
    else:
        if not write:
            print("[Warning] 'GITHUB_TOKEN' or 'GITHUB_TOKEN_READONLY' env vars "
                  "not set which might lead to API rate limiting", file=sys.stderr)
            return {}
        else:
            raise Exception("'GITHUB_TOKEN' env var not set")


@contextmanager
def make_writable(obj: GithubObject) -> Generator:
    # XXX: This switches the read-only token with a potentially writable one
    old_requester = obj._requester  # type: ignore
    repo = get_current_repo(write=True)
    try:
        obj._requester = repo._requester  # type: ignore
        yield
    finally:
        obj._requester = old_requester  # type: ignore


@lru_cache(maxsize=None)
def get_current_repo(write: bool = False) -> Repository:
    gh = get_github(write=write)
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "msys2/msys2-autobuild")
    return gh.get_repo(repo_full_name, lazy=True)


@lru_cache(maxsize=None)
def get_repo(build_type: BuildType, write: bool = False) -> Repository:
    gh = get_github(write=write)
    return gh.get_repo(Config.ASSETS_REPO[build_type], lazy=True)


@lru_cache(maxsize=None)
def get_github(write: bool = False) -> Github:
    kwargs = get_credentials(write=write)
    has_creds = bool(kwargs)
    # 100 is the maximum allowed
    kwargs['per_page'] = 100
    kwargs['retry'] = REQUESTS_RETRY
    kwargs['timeout'] = sum(REQUESTS_TIMEOUT)
    gh = Github(**kwargs)
    if not has_creds and not write:
        print(f"[Warning] Rate limit status: {gh.get_rate_limit().core}", file=sys.stderr)
    return gh


def asset_is_complete(asset: GitReleaseAsset) -> bool:
    # assets can stay around in a weird incomplete state
    # in which case asset.state == "starter". GitHub shows
    # them with a red warning sign in the edit UI.
    return asset.state == "uploaded"


def download_text_asset(asset: GitReleaseAsset) -> str:
    assert asset_is_complete(asset)
    session = get_requests_session()
    with session.get(asset.browser_download_url, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        return r.text


def get_current_run_urls() -> Optional[Dict[str, str]]:
    # The only connection we have is the job name, so this depends
    # on unique job names in all workflows
    if "GITHUB_SHA" in os.environ and "GITHUB_RUN_NAME" in os.environ:
        sha = os.environ["GITHUB_SHA"]
        run_name = os.environ["GITHUB_RUN_NAME"]
        commit = get_current_repo().get_commit(sha)
        check_runs = commit.get_check_runs(
            check_name=run_name, status="in_progress", filter="latest")
        for run in check_runs:
            html = run.html_url + "?check_suite_focus=true"
            raw = commit.html_url + "/checks/" + str(run.id) + "/logs"
            return {"html": html, "raw": raw}
        else:
            raise Exception(f"No active job found for { run_name }")
    return None


def wait_for_api_limit_reset(
        min_remaining_write: int = 50, min_remaining: int = 250, min_sleep: float = 60,
        max_sleep: float = 300) -> None:

    for write in [False, True]:
        gh = get_github(write=write)
        while True:
            core = gh.get_rate_limit().core
            reset = fixup_datetime(core.reset)
            now = datetime.now(timezone.utc)
            diff = (reset - now).total_seconds()
            print(f"{core.remaining} API calls left (write={write}), "
                  f"{diff} seconds until the next reset")
            if core.remaining > (min_remaining_write if write else min_remaining):
                break
            wait = diff
            if wait < min_sleep:
                wait = min_sleep
            elif wait > max_sleep:
                wait = max_sleep
            print(f"Too few API calls left, waiting for {wait} seconds")
            time.sleep(wait)


def fixup_datetime(dt: datetime) -> datetime:
    # pygithub returns datetime objects without a timezone
    # https://github.com/PyGithub/PyGithub/issues/1905
    assert dt.tzinfo is None
    return dt.replace(tzinfo=timezone.utc)


def get_asset_mtime_ns(asset: GitReleaseAsset) -> int:
    """Returns the mtime of an asset in nanoseconds"""

    updated_at = fixup_datetime(asset.updated_at)
    return int(updated_at.timestamp() * (1000 ** 3))


def download_asset(asset: GitReleaseAsset, target_path: str) -> None:
    assert asset_is_complete(asset)
    session = get_requests_session(nocache=True)
    with session.get(asset.browser_download_url, stream=True, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        fd, temppath = tempfile.mkstemp()
        try:
            os.chmod(temppath, 0o644)
            with os.fdopen(fd, "wb") as h:
                for chunk in r.iter_content(4096):
                    h.write(chunk)
            mtime_ns = get_asset_mtime_ns(asset)
            os.utime(temppath, ns=(mtime_ns, mtime_ns))
            shutil.move(temppath, target_path)
        finally:
            try:
                os.remove(temppath)
            except OSError:
                pass


def get_gh_asset_name(basename: PathLike, text: bool = False) -> str:
    # GitHub will throw out charaters like '~' or '='. It also doesn't like
    # when there is no file extension and will try to add one
    return sha256(str(basename).encode("utf-8")).hexdigest() + (".bin" if not text else ".txt")


def get_asset_filename(asset: GitReleaseAsset) -> str:
    if not asset.label:
        return asset.name
    else:
        assert os.path.splitext(get_gh_asset_name(asset.label))[0] == \
            os.path.splitext(asset.name)[0]
        return asset.label


def get_release_assets(release: GitRelease, include_incomplete: bool = False) -> List[GitReleaseAsset]:
    assets = []
    for asset in release.assets:
        # skip in case not fully uploaded yet (or uploading failed)
        if not asset_is_complete(asset) and not include_incomplete:
            continue
        uploader = asset.uploader
        uploader_key = (uploader.type, uploader.login)
        # We allow uploads from some users and GHA
        if uploader_key not in Config.ALLOWED_UPLOADERS:
            raise SystemExit(
                f"ERROR: Asset '{get_asset_filename(asset)}' "
                f"uploaded by {uploader_key}'. Aborting.")
        assets.append(asset)
    return assets


def upload_asset(release: GitRelease, path: PathLike, replace: bool = False,
                 text: bool = False, content: Optional[bytes] = None) -> None:
    path = Path(path)
    basename = os.path.basename(str(path))
    asset_name = get_gh_asset_name(basename, text)
    asset_label = basename

    def can_try_upload_again() -> bool:
        for asset in get_release_assets(release, include_incomplete=True):
            if asset_name == asset.name:
                # We want to treat incomplete assets as if they weren't there
                # so replace them always
                if replace or not asset_is_complete(asset):
                    with make_writable(asset):
                        asset.delete_asset()
                    break
                else:
                    print(f"Skipping upload for {asset_name} as {asset_label}, already exists")
                    return False
        return True

    def upload() -> None:
        with make_writable(release):
            if content is None:
                with open(path, "rb") as fileobj:
                    release.upload_asset_from_memory(  # type: ignore
                        fileobj, os.path.getsize(path), label=asset_label, name=asset_name)
            else:
                with io.BytesIO(content) as fileobj:
                    release.upload_asset_from_memory(  # type: ignore
                        fileobj, len(content), label=asset_label, name=asset_name)

    try:
        upload()
    except (GithubException, requests.RequestException):
        if can_try_upload_again():
            upload()

    print(f"Uploaded {asset_name} as {asset_label}")


def get_release(repo: Repository, name: str, create: bool = True) -> GitRelease:
    """Like Repository.get_release() but creates the referenced release if needed"""

    try:
        return repo.get_release(name)
    except UnknownObjectException:
        if not create:
            raise
        with make_writable(repo):
            return repo.create_git_release(name, name, name, prerelease=True)


class CachedAssets:

    def __init__(self) -> None:
        self._assets: Dict[BuildType, List[GitReleaseAsset]] = {}
        self._failed: Dict[str, List[GitReleaseAsset]] = {}

    def get_assets(self, build_type: BuildType) -> List[GitReleaseAsset]:
        if build_type not in self._assets:
            repo = get_repo(build_type)
            release = get_release(repo, 'staging-' + build_type)
            self._assets[build_type] = get_release_assets(release)
        return self._assets[build_type]

    def get_failed_assets(self, build_type: BuildType) -> List[GitReleaseAsset]:
        repo = get_repo(build_type)
        key = repo.full_name
        if key not in self._failed:
            release = get_release(repo, 'staging-failed')
            self._failed[key] = get_release_assets(release)
        assets = self._failed[key]
        # XXX: This depends on the format of the filename
        return [a for a in assets if get_asset_filename(a).startswith(build_type + "-")]
