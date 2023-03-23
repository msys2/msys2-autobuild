import os
from contextlib import contextmanager
from datetime import timedelta
from functools import lru_cache
from typing import Any, AnyStr, Dict, Generator, List, Union

import requests
from requests.adapters import HTTPAdapter

from .config import REQUESTS_RETRY, REQUESTS_TIMEOUT, Config

PathLike = Union[os.PathLike, AnyStr]
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def requests_cache_disabled() -> Any:
    import requests_cache
    return requests_cache.disabled()


@lru_cache(maxsize=None)
def get_requests_session(nocache=False) -> requests.Session:
    adapter = HTTPAdapter(max_retries=REQUESTS_RETRY)
    if nocache:
        with requests_cache_disabled():
            http = requests.Session()
    else:
        http = requests.Session()
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    return http


@contextmanager
def install_requests_cache() -> Generator:
    # This adds basic etag based caching, to avoid hitting API rate limiting

    import requests_cache
    from requests_cache.backends.sqlite import SQLiteCache

    # Monkey patch globally, so pygithub uses it as well.
    # Only do re-validation with etag/date etc and ignore the cache-control headers that
    # github sends by default with 60 seconds.
    cache_dir = os.path.join(os.getcwd(), '.autobuild_cache')
    os.makedirs(cache_dir, exist_ok=True)
    requests_cache.install_cache(
        always_revalidate=True,
        cache_control=False,
        expire_after=requests_cache.EXPIRE_IMMEDIATELY,
        backend=SQLiteCache(os.path.join(cache_dir, 'http_cache.sqlite')))

    # Call this once, so it gets cached from the main thread and can be used in a thread pool
    get_requests_session(nocache=True)

    try:
        yield
    finally:
        # Delete old cache entries, so this doesn't grow indefinitely
        cache = requests_cache.get_cache()
        assert cache is not None
        cache.delete(older_than=timedelta(hours=3))

        # un-monkey-patch again
        requests_cache.uninstall_cache()


@contextmanager
def gha_group(title: str) -> Generator:
    print(f'\n::group::{title}')
    try:
        yield
    finally:
        print('::endgroup::')


def queue_website_update() -> None:
    session = get_requests_session()
    r = session.post('https://packages.msys2.org/api/trigger_update', timeout=REQUESTS_TIMEOUT)
    try:
        # it's not worth stopping the build if this fails, so just log it
        r.raise_for_status()
    except requests.RequestException as e:
        print(e)


def parse_optional_deps(optional_deps: str) -> Dict[str, List[str]]:
    res: Dict[str, List[str]] = {}
    optional_deps = optional_deps.replace(" ", "")
    if not optional_deps:
        return res
    for entry in optional_deps.split(","):
        assert ":" in entry
        first, second = entry.split(":", 2)
        res.setdefault(first, []).append(second)
    return res


def apply_optional_deps(optional_deps: str) -> None:
    for dep, ignored in parse_optional_deps(optional_deps).items():
        Config.OPTIONAL_DEPS.setdefault(dep, []).extend(ignored)
