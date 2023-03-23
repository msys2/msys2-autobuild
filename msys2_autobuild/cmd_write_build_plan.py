import json
import os
import shlex
from typing import Any, Dict, List

from tabulate import tabulate

from .config import Config
from .gh import (get_current_repo, get_current_workflow,
                 wait_for_api_limit_reset)
from .queue import (Package, PackageStatus, get_buildqueue_with_status,
                    get_cycles, update_status)
from .utils import apply_optional_deps, gha_group


def show_cycles(pkgs: List[Package]) -> None:
    cycles = get_cycles(pkgs)
    if cycles:
        def format_package(p: Package) -> str:
            return f"{p['name']} [{p['version_repo']} -> {p['version']}]"

        with gha_group(f"Dependency Cycles ({len(cycles)})"):
            print(tabulate([
                (format_package(a), "<-->", format_package(b)) for (a, b) in cycles],
                headers=["Package", "", "Package"]))


def get_job_meta() -> List[Dict[str, Any]]:
    hosted_runner = "windows-2022"
    job_meta: List[Dict[str, Any]] = [
        {
            "build-types": ["mingw64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types mingw64",
                "name": "mingw64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["mingw32"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types mingw32",
                "name": "mingw32",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["ucrt64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types ucrt64",
                "name": "ucrt64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clang64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clang64",
                "name": "clang64",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clang32"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clang32",
                "name": "clang32",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["clangarm64"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types clangarm64",
                "name": "clangarm64",
                "runner": ["Windows", "ARM64", "autobuild"]
            }
        }, {
            "build-types": ["msys"],
            "matrix": {
                "packages": "base-devel",
                "build-args": "--build-types msys",
                "name": "msys",
                "runner": hosted_runner
            }
        }, {
            "build-types": ["msys-src", "mingw-src"],
            "matrix": {
                "packages": "base-devel VCS",
                "build-args": "--build-types msys-src,mingw-src",
                "name": "src",
                "runner": hosted_runner
            }
        }
    ]

    return job_meta


def write_build_plan(args: Any) -> None:
    target_file = args.target_file
    optional_deps = args.optional_deps or ""

    apply_optional_deps(optional_deps)

    current_id = None
    if "GITHUB_RUN_ID" in os.environ:
        current_id = int(os.environ["GITHUB_RUN_ID"])

    def write_out(result: List[Dict[str, str]]) -> None:
        with open(target_file, "wb") as h:
            h.write(json.dumps(result).encode())

    workflow = get_current_workflow()
    runs = list(workflow.get_runs(status="in_progress"))
    runs += list(workflow.get_runs(status="queued"))
    for run in runs:
        if current_id is not None and current_id == run.id:
            # Ignore this run itself
            continue
        print(f"Another workflow is currently running or has something queued: {run.html_url}")
        write_out([])
        return

    wait_for_api_limit_reset()

    pkgs = get_buildqueue_with_status(full_details=True)

    show_cycles(pkgs)

    update_status(pkgs)

    queued_build_types: Dict[str, int] = {}
    for pkg in pkgs:
        for build_type in pkg.get_build_types():
            if pkg.get_status(build_type) == PackageStatus.WAITING_FOR_BUILD:
                queued_build_types[build_type] = queued_build_types.get(build_type, 0) + 1

    if not queued_build_types:
        write_out([])
        return

    available_build_types = set()
    for build_type, repo_name in Config.ASSETS_REPO.items():
        if repo_name == get_current_repo().full_name:
            available_build_types.add(build_type)

    jobs = []
    for job_info in get_job_meta():
        matching_build_types = \
            set(queued_build_types) & set(job_info["build-types"]) & available_build_types
        if matching_build_types:
            build_count = sum(queued_build_types[bt] for bt in matching_build_types)
            job = job_info["matrix"]
            # TODO: pin optional deps to their exact version somehow, in case something changes
            # between this run and when the worker gets to it.
            if optional_deps:
                job["build-args"] = job["build-args"] + " --optional-deps " + shlex.quote(optional_deps)
            jobs.append(job)
            # XXX: If there is more than three builds we start two jobs with the second
            # one having a reversed build order
            if build_count > 3:
                matrix = dict(job)
                matrix["build-args"] = matrix["build-args"] + " --build-from end"
                matrix["name"] = matrix["name"] + "-2"
                jobs.append(matrix)
            if build_count > 9:
                matrix = dict(job)
                matrix["build-args"] = matrix["build-args"] + " --build-from middle"
                matrix["name"] = matrix["name"] + "-3"
                jobs.append(matrix)

    write_out(jobs[:Config.MAXIMUM_JOB_COUNT])


def add_parser(subparsers) -> None:
    sub = subparsers.add_parser(
        "write-build-plan", help="Write a GHA build matrix setup", allow_abbrev=False)
    sub.add_argument("--optional-deps", action="store")
    sub.add_argument("target_file")
    sub.set_defaults(func=write_build_plan)
