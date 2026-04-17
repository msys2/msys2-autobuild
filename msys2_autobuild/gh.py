import io
import os
import shutil
import sys
import tempfile
import time
import base64
import hashlib
from contextlib import contextmanager
from datetime import datetime, UTC
from functools import cache
from pathlib import Path
from typing import Any
from collections.abc import Generator, Callable

import requests
from github import Github
from github.Auth import Auth, Token
from github.GithubException import GithubException, UnknownObjectException
from github.GithubObject import GithubObject
from github.GitRelease import GitRelease
from github.GitReleaseAsset import GitReleaseAsset
from github.Artifact import Artifact
from github.Repository import Repository
from gha_artifact_client import ArtifactClientApi

from .config import REQUESTS_TIMEOUT, BuildType, Config
from .utils import PathLike, get_requests_session


def get_auth(write: bool = False) -> Auth | None:
    if not write and os.environ.get("GITHUB_TOKEN_READONLY", ""):
        return Token(os.environ["GITHUB_TOKEN_READONLY"])
    elif "GITHUB_TOKEN" in os.environ:
        return Token(os.environ["GITHUB_TOKEN"])
    else:
        if not write:
            print("[Warning] 'GITHUB_TOKEN' or 'GITHUB_TOKEN_READONLY' env vars "
                  "not set which might lead to API rate limiting", file=sys.stderr)
            return None
        else:
            raise Exception("'GITHUB_TOKEN' env var not set")


@contextmanager
def make_writable(obj: GithubObject) -> Generator:
    # XXX: This switches the read-only token with a potentially writable one
    old_requester = obj._requester
    repo = get_current_repo(write=True)
    try:
        obj._requester = repo._requester
        yield
    finally:
        obj._requester = old_requester


@cache
def _get_repo(name: str, write: bool = False) -> Repository:
    gh = get_github(write=write)
    return gh.get_repo(name, lazy=True)


def get_current_repo(write: bool = False) -> Repository:
    repo_full_name = os.environ.get("GITHUB_REPOSITORY", "msys2/msys2-autobuild")
    return _get_repo(repo_full_name, write)


def get_repo_for_build_type(build_type: BuildType, write: bool = False) -> Repository:
    return _get_repo(Config.RUNNER_CONFIG[build_type]["repo"], write)


@cache
def get_github(write: bool = False) -> Github:
    auth = get_auth(write=write)
    kwargs: dict[str, Any] = {}
    kwargs['auth'] = auth
    # 100 is the maximum allowed
    kwargs['per_page'] = 100
    kwargs['timeout'] = sum(REQUESTS_TIMEOUT)
    kwargs['seconds_between_requests'] = None
    kwargs['lazy'] = True
    gh = Github(**kwargs)
    if auth is None and not write:
        print(f"[Warning] Rate limit status: {gh.get_rate_limit().resources.core}", file=sys.stderr)
    return gh


def asset_is_complete(asset: GitReleaseAsset) -> bool:
    # assets can stay around in a weird incomplete state
    # in which case asset.state == "starter". GitHub shows
    # them with a red warning sign in the edit UI.
    return asset.state == "uploaded"


def download_text_asset(asset: GitReleaseAsset, cache=False) -> str:
    assert asset_is_complete(asset)
    session = get_requests_session(nocache=not cache)
    with session.get(asset.browser_download_url, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        return r.text


def get_workflow_run_id() -> int|None:
    if "GITHUB_RUN_ID" not in os.environ:
        return None
    return int(os.environ["GITHUB_RUN_ID"])


def get_job_check_run_id() -> int|None:
    if "JOB_CHECK_RUN_ID" not in os.environ:
        return None
    return int(os.environ["JOB_CHECK_RUN_ID"])


def get_current_run_urls() -> dict[str, str] | None:
    job_check_run_id = get_job_check_run_id()
    if job_check_run_id is not None:
        repo = get_current_repo()
        run = repo.get_check_run(job_check_run_id)
        html = run.html_url + "?check_suite_focus=true"
        commit = repo.get_commit(run.head_sha)
        raw = commit.html_url + "/checks/" + str(run.id) + "/logs"
        return {"html": html, "raw": raw}
    return None


def wait_for_api_limit_reset(
        min_remaining_write: int = 50, min_remaining: int = 250, min_sleep: float = 60,
        max_sleep: float = 300) -> None:

    for write in [False, True]:
        gh = get_github(write=write)
        while True:
            core = gh.get_rate_limit().resources.core
            reset = core.reset
            now = datetime.now(UTC)
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


def get_asset_mtime_ns(asset: GitReleaseAsset) -> int:
    """Returns the mtime of an asset in nanoseconds"""

    return int(asset.updated_at.timestamp() * (1000 ** 3))


def download_artifact(artifact: Artifact) -> bytes:
    """Downloads the artifact and returns its content as bytes"""

    assert not artifact.expired

    # "You must have the actions scope to download artifacts"
    with make_writable(artifact):
        requester = artifact.requester
    status, responseHeaders, output = requester.requestBlob("GET",
        artifact.archive_download_url, headers={"Accept": "application/vnd.github+json"})
    assert status == 302 and output == ""

    # Now download the signed URL and verify the digest
    session = get_requests_session(nocache=True)
    with session.get(responseHeaders["location"], timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        content = r.content
        with verify_artifact_digest(artifact) as hash:
            hash.update(content)
        return content


def download_asset(asset: GitReleaseAsset, target_path: str,
                   onverify: Callable[[str, str], None] | None = None) -> None:
    assert asset_is_complete(asset)
    session = get_requests_session(nocache=True)
    with session.get(asset.browser_download_url, stream=True, timeout=REQUESTS_TIMEOUT) as r:
        r.raise_for_status()
        fd, temppath = tempfile.mkstemp()
        try:
            os.chmod(temppath, 0o644)
            with verify_asset_digest(asset) as hash:
                with os.fdopen(fd, "wb") as h:
                    for chunk in r.iter_content(256 * 1024):
                        hash.update(chunk)
                        h.write(chunk)
            mtime_ns = get_asset_mtime_ns(asset)
            os.utime(temppath, ns=(mtime_ns, mtime_ns))
            if onverify is not None:
                onverify(temppath, target_path)
            shutil.move(temppath, target_path)
        finally:
            try:
                os.remove(temppath)
            except OSError:
                pass


def encode_filename(filename: str) -> str:
    """Encode to something which is valid in GH asset names and artifact names"""
    return base64.urlsafe_b64encode(filename.encode()).decode().rstrip('=')


def decode_filename(encoded: str) -> str:
    """Decode a filename encoded with encode_filename"""
    padding = '=' * (4 - len(encoded) % 4) if len(encoded) % 4 else ''
    return base64.urlsafe_b64decode(encoded + padding).decode()


def get_upload_safe_name(basename: PathLike, text: bool = False) -> str:
    # GitHub will throw out characters like '~' or '='. It also doesn't like
    # when there is no file extension and will try to add one
    name = str(basename)
    if not name:
        # leading "." would not work
        raise ValueError("Asset name cannot be empty")
    return encode_filename(name) + (".bin" if not text else ".txt")


def get_asset_filename(asset: GitReleaseAsset) -> str:
    assert asset.label
    assert os.path.splitext(get_upload_safe_name(asset.label))[0] == \
        os.path.splitext(asset.name)[0]
    return asset.label


def get_artifact_filename(artifact: Artifact) -> str:
    return decode_filename(artifact.name.rsplit(".", 1)[0])


@contextmanager
def verify_asset_digest(asset: GitReleaseAsset) -> Generator[Any, None, None]:
    digest = asset.digest
    if digest is None:
        raise Exception(f"Asset {get_asset_filename(asset)} has no digest")
    type_, value = digest.split(":", 1)
    value = value.lower()
    h = hashlib.new(type_)
    try:
        yield h
    finally:
        hexdigest = h.hexdigest().lower()
        if h.hexdigest() != value:
            raise Exception(f"Digest mismatch for asset {get_asset_filename(asset)}: "
                            f"got {hexdigest}, expected {value}")

@contextmanager
def verify_artifact_digest(artifact: Artifact) -> Generator[Any, None, None]:
    digest = artifact.digest
    type_, value = digest.split(":", 1)
    value = value.lower()
    h = hashlib.new(type_)
    try:
        yield h
    finally:
        hexdigest = h.hexdigest().lower()
        if h.hexdigest() != value:
            raise Exception(f"Digest mismatch for asset {get_artifact_filename(artifact)}: "
                            f"got {hexdigest}, expected {value}")


def is_asset_from_gha(asset: GitReleaseAsset) -> bool:
    """If the asset was uploaded from CI via GHA"""

    uploader = asset.uploader
    return uploader.type == "Bot" and uploader.login == "github-actions[bot]"


def is_asset_from_allowed_user(asset: GitReleaseAsset) -> bool:
    """If the asset was uploaded by an allowed user"""

    uploader = asset.uploader
    return uploader.type == "User" and uploader.login in Config.ALLOWED_UPLOADERS


def get_asset_uploader_name(asset: GitReleaseAsset) -> str:
    """Returns the name of the user that uploaded the asset"""

    uploader = asset.uploader
    return uploader.login


def get_release_assets(release: GitRelease, include_incomplete: bool = False) -> list[GitReleaseAsset]:
    assets = []
    for asset in release.assets:
        # skip in case not fully uploaded yet (or uploading failed)
        if not asset_is_complete(asset) and not include_incomplete:
            continue
        # We allow uploads from GHA and some special users
        if not is_asset_from_gha(asset) and not is_asset_from_allowed_user(asset):
            raise SystemExit(
                f"ERROR: Asset '{get_asset_filename(asset)}' "
                f"uploaded by {get_asset_uploader_name(asset)}'. Aborting.")
        assets.append(asset)
    return assets


def is_running_in_gha() -> bool:
    return "GITHUB_ACTIONS" in os.environ and os.environ["GITHUB_ACTIONS"] == "true"


def can_upload_artifacts() -> bool:
    return "ACTIONS_RUNTIME_TOKEN" in os.environ and "ACTIONS_RESULTS_URL" in os.environ


def upload_artifact(path: PathLike, text: bool = False, retention_hours: int = 7,
                    content: bytes | None = None) -> None:
    assert can_upload_artifacts()

    path = Path(path)
    basename = os.path.basename(str(path))
    asset_name = get_upload_safe_name(basename, text)

    client = ArtifactClientApi()
    if content is None:
        result = client.upload_artifact(
            path, name=asset_name, expires_in=3600 * retention_hours)
    else:
        result = client.upload_artifact_bytes(
            content, name=asset_name, expires_in=3600 * retention_hours)
    print(f"Uploaded {asset_name} as {result.id}")


def upload_asset(release: GitRelease, path: PathLike, replace: bool = False,
                 text: bool = False, content: bytes | None = None) -> None:
    path = Path(path)
    basename = os.path.basename(str(path))
    asset_name = get_upload_safe_name(basename, text)
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
                    release.upload_asset_from_memory(
                        fileobj, os.path.getsize(path), label=asset_label, name=asset_name)
            else:
                with io.BytesIO(content) as fileobj:
                    release.upload_asset_from_memory(
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
        self._assets: dict[BuildType, list[GitReleaseAsset]] = {}
        self._failed: dict[str, list[GitReleaseAsset]] = {}

    def get_assets(self, build_type: BuildType) -> list[GitReleaseAsset]:
        if build_type not in self._assets:
            repo = get_repo_for_build_type(build_type)
            release = get_release(repo, 'staging-' + build_type)
            self._assets[build_type] = get_release_assets(release)
        return self._assets[build_type]

    def get_failed_assets(self, build_type: BuildType) -> list[GitReleaseAsset]:
        repo = get_repo_for_build_type(build_type)
        key = repo.full_name
        if key not in self._failed:
            release = get_release(repo, 'staging-failed')
            self._failed[key] = get_release_assets(release)
        assets = self._failed[key]
        # XXX: This depends on the format of the filename
        return [a for a in assets if get_asset_filename(a).startswith(build_type + "-")]
