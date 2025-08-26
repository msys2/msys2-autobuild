import json
import shlex
from typing import Any
from collections.abc import Iterator
import itertools

from .config import BuildType, Config, build_type_is_src
from .gh import get_current_repo, wait_for_api_limit_reset
from .queue import (Package, PackageStatus, get_buildqueue_with_status,
                    update_status)
from .utils import apply_optional_deps


def generate_jobs_for(build_type: BuildType, optional_deps: str, count: int) -> Iterator[dict[str, Any]]:
    name = build_type
    packages = " ".join(["base-devel"])
    runner = Config.RUNNER_CONFIG[build_type]["labels"]
    hosted = Config.RUNNER_CONFIG[build_type]["hosted"]
    build_from = itertools.cycle(["start", "end", "middle"])
    for i in range(count):
        real_name = name if i == 0 else name + "-" + str(i + 1)
        build_args = ["--build-types", build_type, "--build-from", next(build_from)]
        if optional_deps:
            build_args += ["--optional-deps", optional_deps]
        yield {
            "name": real_name,
            "packages": packages,
            "runner": runner,
            "hosted": hosted,
            "build-args": shlex.join(build_args),
        }


def generate_src_jobs(optional_deps: str, count: int) -> Iterator[dict[str, Any]]:
    name = "src"
    packages = " ".join(["base-devel", "VCS"])
    build_types = [Config.MINGW_SRC_BUILD_TYPE, Config.MSYS_SRC_BUILD_TYPE]
    runner = Config.RUNNER_CONFIG[build_types[0]]["labels"]
    hosted = Config.RUNNER_CONFIG[build_types[0]]["hosted"]
    build_from = itertools.cycle(["start", "end", "middle"])
    for i in range(count):
        real_name = name if i == 0 else name + "-" + str(i + 1)
        build_args = ["--build-types", ",".join(build_types), "--build-from", next(build_from)]
        if optional_deps:
            build_args += ["--optional-deps", optional_deps]
        yield {
            "name": real_name,
            "packages": packages,
            "runner": runner,
            "hosted": hosted,
            "build-args": shlex.join(build_args),
        }


# from https://docs.python.org/3/library/itertools.html
def roundrobin(*iterables):  # type: ignore
    "roundrobin('ABC', 'D', 'EF') --> A D E B F C"
    # Recipe credited to George Sakkis
    num_active = len(iterables)
    nexts = itertools.cycle(iter(it).__next__ for it in iterables)
    while num_active:
        try:
            for next in nexts:
                yield next()
        except StopIteration:
            # Remove the iterator we just exhausted from the cycle.
            num_active -= 1
            nexts = itertools.cycle(itertools.islice(nexts, num_active))


def create_build_plan(pkgs: list[Package], optional_deps: str) -> list[dict[str, Any]]:
    queued_build_types: dict[BuildType, int] = {}
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            # skip if we can't build it
            if Config.RUNNER_CONFIG[build_type]["repo"] != get_current_repo().full_name:
                continue
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                queued_build_types[build_type] = queued_build_types.get(build_type, 0) + 1

    def get_job_count(build_type: BuildType) -> int:
        queued = queued_build_types[build_type]
        if queued > 9:
            count = 3
        elif queued > 3:
            count = 2
        else:
            count = 1
        return min(Config.RUNNER_CONFIG[build_type].get("max_jobs", count), count)

    # generate the build jobs
    job_lists = []
    for build_type, count in queued_build_types.items():
        if build_type_is_src(build_type):
            continue
        count = get_job_count(build_type)
        job_lists.append(list(generate_jobs_for(build_type, optional_deps, count)))
    jobs = list(roundrobin(*job_lists))[:Config.MAXIMUM_JOB_COUNT]

    # generate src build jobs
    src_build_types = [
        b for b in [Config.MINGW_SRC_BUILD_TYPE, Config.MSYS_SRC_BUILD_TYPE]
        if b in queued_build_types]
    if src_build_types:
        src_count = min(get_job_count(b) for b in src_build_types)
        jobs.extend(list(generate_src_jobs(optional_deps, src_count)))

    return jobs


def write_build_plan(args: Any) -> None:
    target_file = args.target_file
    optional_deps = args.optional_deps or ""

    apply_optional_deps(optional_deps)

    def write_out(result: list[dict[str, Any]]) -> None:
        with open(target_file, "wb") as h:
            h.write(json.dumps(result).encode())

    wait_for_api_limit_reset()

    pkgs = get_buildqueue_with_status(full_details=True)

    update_status(pkgs)

    jobs = create_build_plan(pkgs, optional_deps)

    write_out(jobs)


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "write-build-plan", help="Write a GHA build matrix setup", allow_abbrev=False)
    sub.add_argument("--optional-deps", action="store")
    sub.add_argument("target_file")
    sub.set_defaults(func=write_build_plan)
