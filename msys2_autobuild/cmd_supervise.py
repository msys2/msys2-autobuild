import json
import fnmatch
import time
import traceback
from typing import Any

from github.Artifact import Artifact

from .asset_cleanup import clean_assets
from .build_plan import create_build_plan
from .buildqueue_report import show_buildqueue
from .gh import create_dispatch, download_artifact, get_artifact_filename, \
    get_current_repo, get_release, get_workflow_run_id, make_writable, upload_asset, \
    wait_for_api_limit_reset
from .queue import get_buildqueue_with_status, update_status, get_build_jobs_status
from .utils import apply_optional_deps


def supervise(args: Any) -> None:
    dry_run = args.dry_run
    repo = get_current_repo()
    branch = args.target_branch
    optional_deps = args.optional_deps or ""

    apply_optional_deps(optional_deps)
    wait_for_api_limit_reset()

    pkgs = get_buildqueue_with_status(full_details=True)
    update_status(pkgs)

    build_plan = create_build_plan(pkgs, optional_deps, bool(optional_deps))
    if not build_plan:
        print("No build jobs to dispatch.")
        return

    clean_assets(dry_run=dry_run)
    show_buildqueue(get_buildqueue_with_status())

    def wait_for_jobs(workflow_run_id: int) -> None:
        while True:
            run = repo.get_workflow_run(workflow_run_id)
            if list(run.jobs()):
                return
            if run.conclusion:
                print(f"Warning: dispatched workflow run {workflow_run_id} completed without any jobs")
                return
            print("Waiting for dispatched workflow jobs to appear...")
            time.sleep(5)

    workflow = repo.get_workflow("build-jobs.yml")
    with make_writable(workflow):
        workflow_run = create_dispatch(
            workflow, branch, inputs={"build-plan": json.dumps(build_plan)})
    workflow_run_id = workflow_run.id
    next_supervisor_dispatched = False
    wait_for_jobs(workflow_run_id)

    def deploy_artifacts(artifacts: list[Artifact]) -> bool:
        """Upload the artifacts to the releases and delete them from the workflow run.
        Returns True if any artifacts were uploaded."""

        if not artifacts:
            return False

        # For each release, find the matching artifacts
        artifacts_map = {get_artifact_filename(artifact): artifact for artifact in artifacts}
        pkgs = get_buildqueue_with_status()
        release_map: dict[str, list[Artifact]] = {}
        all_matched= []
        for pkg in pkgs:
            for build_type in pkg.get_build_types():
                matches = []
                for pattern in pkg.get_build_patterns(build_type):
                    matches.extend(fnmatch.filter(artifacts_map.keys(), pattern))
                matched = [artifacts_map[match] for match in matches]
                release_map.setdefault('staging-' + build_type, []).extend(matched)
                all_matched.extend(matched)

                matches = []
                for pattern in pkg.get_failed_patterns(build_type):
                    matches.extend(fnmatch.filter(artifacts_map.keys(), pattern))
                matched = [artifacts_map[match] for match in matches]
                release_map.setdefault('staging-failed', []).extend(matched)
                all_matched.extend(matched)

        # Delete all artifacts that did not match any release pattern
        for artifact in artifacts:
            if artifact not in all_matched:
                print(f"Warning: artifact {get_artifact_filename(artifact)} did not match. Deleting it.")
                if not dry_run:
                    with make_writable(artifact):
                        artifact.delete()

        # Upload the artifacts to the releases and delete them from the workflow run
        changed = False
        for release_name, artifacts in release_map.items():
            release = get_release(repo, release_name)
            for artifact in artifacts:
                changed = True
                data = download_artifact(artifact)
                filename = get_artifact_filename(artifact)
                print(f"Uploading {filename} to release {release_name}")
                if not dry_run:
                    upload_asset(release, filename, content=data)
                print(f"Deleting artifact {filename}")
                if not dry_run:
                    with make_writable(artifact):
                        artifact.delete()

        return changed

    jobs_status = []
    while True:
        wait_for_api_limit_reset()

        run = repo.get_workflow_run(workflow_run_id)
        jobs = list(run.jobs())
        all_jobs_done = all(job.conclusion for job in jobs)

        try:
            artifacts = list(run.get_artifacts())
            was_deployed = deploy_artifacts(artifacts)

            new_jobs_status = get_build_jobs_status(
                [job for job in jobs if not job.conclusion])
            status_changed = False
            if new_jobs_status != jobs_status:
                jobs_status = new_jobs_status
                status_changed = True

            if was_deployed or status_changed:
                print("Updating build queue status...")
                pkgs = get_buildqueue_with_status(full_details=True)
                if not dry_run:
                    update_status(pkgs)

                if not next_supervisor_dispatched:
                    build_plan = create_build_plan(pkgs, optional_deps, False)
                    if build_plan:
                        supervisor_workflow = repo.get_workflow("build.yml")
                        with make_writable(supervisor_workflow):
                            supervisor_run = create_dispatch(
                                supervisor_workflow,
                                repo.default_branch,
                                inputs={
                                    "context": f"Started by supervisor run {get_workflow_run_id()}",
                                },
                            )
                        wait_for_jobs(supervisor_run.id)
                        next_supervisor_dispatched = True
        except Exception:
            traceback.print_exc()
            print("Error while supervising, will retry in 5 minutes...")
            time.sleep(300)
            continue

        if not all_jobs_done:
            print("Build jobs are still running, checking again in 30 seconds...")
            time.sleep(30)
        else:
            print("Build jobs are completed, stopping supervision.")
            break


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "supervise", help="Plan, dispatch, and supervise build jobs", allow_abbrev=False)
    sub.add_argument(
        "--target-branch", type=str, help="Branch to build in", required=True)
    sub.add_argument("--optional-deps", action="store")
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be uploaded")
    sub.set_defaults(func=supervise)
