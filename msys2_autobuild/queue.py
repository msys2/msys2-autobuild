import fnmatch
import io
import json
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, cast

import requests
from github.GithubException import GithubException

from .config import (REQUESTS_TIMEOUT, ArchType, BuildType, Config,
                     build_type_is_src, get_all_build_types)
from .gh import (CachedAssets, download_text_asset, get_asset_filename,
                 get_current_repo, get_release, make_writable,
                 asset_is_complete)
from .utils import get_requests_session, queue_website_update


class PackageStatus(Enum):
    FINISHED = 'finished'
    FINISHED_BUT_BLOCKED = 'finished-but-blocked'
    FINISHED_BUT_INCOMPLETE = 'finished-but-incomplete'
    FAILED_TO_BUILD = 'failed-to-build'
    WAITING_FOR_BUILD = 'waiting-for-build'
    WAITING_FOR_DEPENDENCIES = 'waiting-for-dependencies'
    MANUAL_BUILD_REQUIRED = 'manual-build-required'
    UNKNOWN = 'unknown'

    def __str__(self) -> str:
        return self.value


class Package(dict):

    def __repr__(self) -> str:
        return "Package({!r})".format(self["name"])

    def __hash__(self) -> int:  # type: ignore
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    @property
    def _active_builds(self) -> dict:
        return {
            k: v for k, v in self["builds"].items() if k in (Config.MINGW_ARCH_LIST + Config.MSYS_ARCH_LIST)}

    def _get_build(self, build_type: BuildType) -> dict:
        return self["builds"].get(build_type, {})

    def get_status(self, build_type: BuildType) -> PackageStatus:
        build = self._get_build(build_type)
        return build.get("status", PackageStatus.UNKNOWN)

    def get_status_details(self, build_type: BuildType) -> dict[str, Any]:
        build = self._get_build(build_type)
        return dict(build.get("status_details", {}))

    def set_status(self, build_type: BuildType, status: PackageStatus,
                   description: str | None = None,
                   urls: dict[str, str] | None = None) -> None:
        build = self["builds"].setdefault(build_type, {})
        build["status"] = status
        meta: dict[str, Any] = {}
        meta["desc"] = description
        if urls is None:
            urls = {}
        meta["urls"] = urls
        build["status_details"] = meta

    def set_blocked(
            self, build_type: BuildType, status: PackageStatus,
            dep: "Package", dep_type: BuildType) -> None:
        dep_details = dep.get_status_details(dep_type)
        dep_blocked = dep_details.get("blocked", {})
        details = self.get_status_details(build_type)
        blocked = details.get("blocked", {})
        if dep_blocked:
            blocked = dict(dep_blocked)
        else:
            blocked.setdefault(dep, set()).add(dep_type)
        descs = []
        for pkg, types in blocked.items():
            descs.append("{} ({})".format(pkg["name"], "/".join(types)))
        self.set_status(build_type, status, "Blocked by: " + ", ".join(descs))
        build = self._get_build(build_type)
        build.setdefault("status_details", {})["blocked"] = blocked

    def is_new(self, build_type: BuildType) -> bool:
        build = self._get_build(build_type)
        return build.get("new", False)

    def get_build_patterns(self, build_type: BuildType) -> list[str]:
        patterns = []
        if build_type_is_src(build_type):
            patterns.append(f"{self['name']}-{self['version']}.src.tar.[!s]*")
        elif build_type in (Config.MINGW_ARCH_LIST + Config.MSYS_ARCH_LIST):
            for item in self._get_build(build_type).get('packages', []):
                patterns.append(f"{item}-{self['version']}-*.pkg.tar.zst")
        else:
            assert 0
        return patterns

    def get_failed_name(self, build_type: BuildType) -> str:
        return f"{build_type}-{self['name']}-{self['version']}.failed"

    def get_build_types(self) -> list[BuildType]:
        build_types = list(self._active_builds)
        if self["source"]:
            if any((k in Config.MINGW_ARCH_LIST) for k in build_types):
                build_types.append(Config.MINGW_SRC_BUILD_TYPE)
            if any((k in Config.MSYS_ARCH_LIST) for k in build_types):
                build_types.append(Config.MSYS_SRC_BUILD_TYPE)
        return build_types

    def _get_dep_build(self, build_type: BuildType) -> dict:
        if build_type == Config.MINGW_SRC_BUILD_TYPE:
            build_type = Config.MINGW_SRC_ARCH
        elif build_type == Config.MSYS_SRC_BUILD_TYPE:
            build_type = Config.MSYS_SRC_ARCH
        return self._get_build(build_type)

    def is_optional_dep(self, dep: "Package", dep_type: BuildType) -> bool:
        # Some deps are manually marked as optional to break cycles.
        # This requires them to be in the main repo though, otherwise the cycle has to
        # be fixed manually.
        return dep["name"] in Config.OPTIONAL_DEPS.get(self["name"], []) and not dep.is_new(dep_type)

    def get_depends(self, build_type: BuildType) -> "dict[ArchType, set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-depends', {})

    def get_rdepends(self, build_type: BuildType) -> "dict[ArchType, set[Package]]":
        build = self._get_dep_build(build_type)
        return build.get('ext-rdepends', {})


def get_buildqueue() -> list[Package]:
    session = get_requests_session()
    r = session.get("https://packages.msys2.org/api/buildqueue2", timeout=REQUESTS_TIMEOUT)
    r.raise_for_status()

    return parse_buildqueue(r.text)


def parse_buildqueue(payload: str) -> list[Package]:
    pkgs = []
    for received in json.loads(payload):
        pkg = Package(received)
        pkg['repo'] = pkg['repo_url'].split('/')[-1]
        pkgs.append(pkg)

    # extract the package mapping
    dep_mapping = {}
    for pkg in pkgs:
        for build in pkg._active_builds.values():
            for name in build['packages']:
                dep_mapping[name] = pkg

    # link up dependencies with the real package in the queue
    for pkg in pkgs:
        for build in pkg._active_builds.values():
            ver_depends: dict[str, set[Package]] = {}
            for repo, deps in build['depends'].items():
                for dep in deps:
                    ver_depends.setdefault(repo, set()).add(dep_mapping[dep])
            build['ext-depends'] = ver_depends

    # reverse dependencies
    for pkg in pkgs:
        for build in pkg._active_builds.values():
            r_depends: dict[str, set[Package]] = {}
            for pkg2 in pkgs:
                for r_repo, build2 in pkg2._active_builds.items():
                    for repo, deps in build2['ext-depends'].items():
                        if pkg in deps:
                            r_depends.setdefault(r_repo, set()).add(pkg2)
            build['ext-rdepends'] = r_depends

    return pkgs


def get_cycles(pkgs: list[Package]) -> set[tuple[Package, Package]]:
    cycles: set[tuple[Package, Package]] = set()

    # In case the package is already built it doesn't matter if it is part of a cycle
    def pkg_is_finished(pkg: Package, build_type: BuildType) -> bool:
        return pkg.get_status(build_type) in [
            PackageStatus.FINISHED,
            PackageStatus.FINISHED_BUT_BLOCKED,
            PackageStatus.FINISHED_BUT_INCOMPLETE,
        ]

    # Transitive dependencies of a package. Excluding branches where a root is finished
    def get_buildqueue_deps(pkg: Package, build_type: ArchType) -> "dict[ArchType, set[Package]]":
        start = (build_type, pkg)
        todo = set([start])
        done = set()
        result = set()

        while todo:
            build_type, pkg = todo.pop()
            item = (build_type, pkg)
            done.add(item)
            if pkg_is_finished(pkg, build_type):
                continue
            result.add(item)
            for dep_build_type, deps in pkg.get_depends(build_type).items():
                for dep in deps:
                    dep_item = (dep_build_type, dep)
                    if dep_item not in done:
                        todo.add(dep_item)
        result.discard(start)

        d: dict[ArchType, set[Package]] = {}
        for build_type, pkg in result:
            d.setdefault(build_type, set()).add(pkg)
        return d

    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if build_type_is_src(build_type):
                continue
            build_type = cast(ArchType, build_type)
            for dep_build_type, deps in get_buildqueue_deps(pkg, build_type).items():
                for dep in deps:
                    # manually broken cycle
                    if pkg.is_optional_dep(dep, dep_build_type) or dep.is_optional_dep(pkg, build_type):
                        continue
                    dep_deps = get_buildqueue_deps(dep, dep_build_type)
                    if pkg in dep_deps.get(build_type, set()):
                        cycles.add(tuple(sorted([pkg, dep], key=lambda p: p["name"])))  # type: ignore

    return cycles


def get_buildqueue_with_status(full_details: bool = False) -> list[Package]:
    cached_assets = CachedAssets()

    assets_failed = []
    for build_type in get_all_build_types():
        assets_failed.extend(cached_assets.get_failed_assets(build_type))

    failed_urls = {}
    if full_details:
        # This might take a while, so only in full mode
        with ThreadPoolExecutor(8) as executor:
            for i, (asset, content) in enumerate(
                    zip(assets_failed, executor.map(download_text_asset, assets_failed))):
                result = json.loads(content)
                if result["urls"]:
                    failed_urls[get_asset_filename(asset)] = result["urls"]

    def pkg_is_done(build_type: BuildType, pkg: Package) -> bool:
        done_names = [get_asset_filename(a) for a in cached_assets.get_assets(build_type)]
        for pattern in pkg.get_build_patterns(build_type):
            if not fnmatch.filter(done_names, pattern):
                return False
        return True

    def get_failed_urls(build_type: BuildType, pkg: Package) -> dict[str, str] | None:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        name = pkg.get_failed_name(build_type)
        if name in failed_names:
            return failed_urls.get(name)
        return None

    def pkg_has_failed(build_type: BuildType, pkg: Package) -> bool:
        failed_names = [get_asset_filename(a) for a in assets_failed]
        name = pkg.get_failed_name(build_type)
        return name in failed_names

    def pkg_is_manual(build_type: BuildType, pkg: Package) -> bool:
        if build_type_is_src(build_type):
            return False
        for pattern, types in Config.MANUAL_BUILD:
            type_matches = not types or build_type in types
            if type_matches and fnmatch.fnmatchcase(pkg['name'], pattern):
                return True
        return False

    pkgs = get_buildqueue()

    # basic state
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg_is_done(build_type, pkg):
                pkg.set_status(build_type, PackageStatus.FINISHED)
            elif pkg_has_failed(build_type, pkg):
                urls = get_failed_urls(build_type, pkg)
                pkg.set_status(build_type, PackageStatus.FAILED_TO_BUILD, urls=urls)
            elif pkg_is_manual(build_type, pkg):
                pkg.set_status(build_type, PackageStatus.MANUAL_BUILD_REQUIRED)
            else:
                pkg.set_status(build_type, PackageStatus.WAITING_FOR_BUILD)

    # wait for dependencies to be finished before starting a build
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            status = pkg.get_status(build_type)
            if status == PackageStatus.WAITING_FOR_BUILD:

                for dep_type, deps in pkg.get_depends(build_type).items():
                    for dep in deps:
                        dep_status = dep.get_status(dep_type)
                        if dep_status != PackageStatus.FINISHED:
                            if pkg.is_optional_dep(dep, dep_type):
                                continue
                            pkg.set_blocked(
                                build_type, PackageStatus.WAITING_FOR_DEPENDENCIES, dep, dep_type)

    # Block packages where not all deps/rdeps/related are finished
    changed = True
    while changed:
        changed = False
        for pkg in pkgs:
            for build_type in pkg.get_build_types():
                status = pkg.get_status(build_type)
                if status == PackageStatus.FINISHED:
                    # src builds are independent
                    if build_type_is_src(build_type):
                        continue

                    for dep_type, deps in pkg.get_depends(build_type).items():
                        for dep in deps:
                            dep_status = dep.get_status(dep_type)
                            if dep_status != PackageStatus.FINISHED:
                                pkg.set_blocked(
                                    build_type, PackageStatus.FINISHED_BUT_BLOCKED, dep, dep_type)
                                changed = True

                    for dep_type, deps in pkg.get_rdepends(build_type).items():
                        for dep in deps:
                            if dep["name"] in Config.IGNORE_RDEP_PACKAGES:
                                continue
                            dep_status = dep.get_status(dep_type)
                            dep_new = dep.is_new(dep_type)
                            # if the rdep isn't in the repo we can't break it by uploading
                            if dep_status != PackageStatus.FINISHED and not dep_new:
                                pkg.set_blocked(
                                    build_type, PackageStatus.FINISHED_BUT_BLOCKED, dep, dep_type)
                                changed = True

        # Block packages where not every build type is finished
        for pkg in pkgs:
            unfinished = []
            blocked = []
            finished = []
            for build_type in pkg.get_build_types():
                status = pkg.get_status(build_type)
                if status != PackageStatus.FINISHED:
                    if status == PackageStatus.FINISHED_BUT_BLOCKED:
                        blocked.append(build_type)
                    # if the package isn't in the repo better not block on it
                    elif not pkg.is_new(build_type):
                        unfinished.append(build_type)
                else:
                    finished.append(build_type)

            # We track source packages by assuming they are in the repo if there is
            # at least one binary package in the repo. Uploading lone source
            # packages will not change anything, so block them.
            if not blocked and not unfinished and finished and \
                    all(build_type_is_src(bt) for bt in finished):
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED):
                        changed = True
                        pkg.set_status(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE)
            elif unfinished:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status in (PackageStatus.FINISHED, PackageStatus.FINISHED_BUT_BLOCKED):
                        changed = True
                        for bt in unfinished:
                            pkg.set_blocked(build_type, PackageStatus.FINISHED_BUT_INCOMPLETE, pkg, bt)
            elif blocked:
                for build_type in pkg.get_build_types():
                    status = pkg.get_status(build_type)
                    if status == PackageStatus.FINISHED:
                        changed = True
                        for bt in blocked:
                            pkg.set_blocked(build_type, PackageStatus.FINISHED_BUT_BLOCKED, pkg, bt)

    return pkgs


def get_status(pkgs: list[Package]) -> dict[str, Any]:
    status_object: dict[str, Any] = {}

    # All currently running jobs
    all_jobs = []
    repo = get_current_repo()
    workflow_runs = repo.get_workflow_runs(status="in_progress")
    for run in workflow_runs:
        jobs = run.jobs("all")
        for job in jobs:
            if job.status == "in_progress":
                all_jobs.append({
                    "name": job.name,
                    "html_url": job.html_url,
                    "started_at": job.started_at.isoformat()
                })
    status_object["jobs"] = sorted(all_jobs, key=lambda j: (j["started_at"], j["html_url"]))

    packages = []
    for pkg in pkgs:
        pkg_result = {}
        pkg_result["name"] = pkg["name"]
        pkg_result["version"] = pkg["version"]
        builds = {}
        for build_type in pkg.get_build_types():
            details = pkg.get_status_details(build_type)
            details.pop("blocked", None)
            details["status"] = pkg.get_status(build_type).value
            builds[build_type] = details
        pkg_result["builds"] = builds
        packages.append(pkg_result)
    status_object["packages"] = packages

    cycles = []
    for a, b in get_cycles(pkgs):
        cycles.append([a["name"], b["name"]])
    status_object["cycles"] = sorted(cycles)

    return status_object


def update_status(pkgs: list[Package]) -> None:
    repo = get_current_repo()
    release = get_release(repo, "status")

    status_object = get_status(pkgs)
    content = json.dumps(status_object, indent=2).encode()

    # If multiple jobs update this at the same time things can fail,
    # assume the other one went through and just ignore all errors
    try:
        asset = None
        asset_name = "status.json"
        for asset in release.assets:
            if asset.name == asset_name:
                break

        do_replace = True

        # Avoid uploading the same file twice, to reduce API write calls
        if asset is not None and asset_is_complete(asset) and asset.size == len(content):
            try:
                old_content = download_text_asset(asset, cache=True)
                if old_content == content.decode():
                    do_replace = False
            except requests.RequestException:
                # github sometimes returns 404 for a short time after uploading
                pass

        if do_replace:
            if asset is not None:
                with make_writable(asset):
                    asset.delete_asset()

            with io.BytesIO(content) as fileobj:
                with make_writable(release):
                    new_asset = release.upload_asset_from_memory(  # type: ignore
                        fileobj, len(content), asset_name)

            package_count = len(status_object['packages'])
            print(f"Uploaded status file for {package_count} packages: {new_asset.browser_download_url}")
            queue_website_update()
        else:
            print("Status unchanged")
    except (GithubException, requests.RequestException) as e:
        print(e)
