from typing import Any
import fnmatch
import time
import traceback

from github.Artifact import Artifact

from .gh import get_current_repo, get_artifact_filename, get_release, \
    download_artifact, upload_asset, make_writable, wait_for_api_limit_reset, \
    get_workflow_run_id, get_job_check_run_id
from .queue import get_buildqueue_with_status, update_status, get_build_jobs_status


def supervise(args: Any) -> None:
    dry_run = args.dry_run
    repo = get_current_repo()

    if args.workflow_run_id is None:
        env_run_id = get_workflow_run_id()
        if env_run_id is None:
            print("Error: --workflow-run-id not specified and GITHUB_RUN_ID env var not set")
            return
        workflow_run_id = env_run_id
    else:
        workflow_run_id = args.workflow_run_id

    def deploy_artifacts(artifacts: list[Artifact]) -> bool:
        """Upload the artifacts to the releases and delete them from the workflow run.
        Returns True if any artifacts were uploaded."""

        if not artifacts:
            return False

        # For each release, find the matching artifacts
        artifacts_map = {get_artifact_filename(artifact): artifact for artifact in artifacts}
        pkgs = get_buildqueue_with_status()
        release_map: dict[str, list[Artifact]] = {}
        for pkg in pkgs:
            for build_type in pkg.get_build_types():
                matches = []
                for pattern in pkg.get_build_patterns(build_type):
                    matches.extend(fnmatch.filter(artifacts_map.keys(), pattern))
                release_map.setdefault(
                    'staging-' + build_type, []).extend([artifacts_map[match] for match in matches])

                matches = []
                for pattern in pkg.get_failed_patterns(build_type):
                    matches.extend(fnmatch.filter(artifacts_map.keys(), pattern))
                release_map.setdefault(
                    'staging-failed', []).extend([artifacts_map[match] for match in matches])

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

    run = repo.get_workflow_run(workflow_run_id)
    jobs_status = []
    while True:
        wait_for_api_limit_reset()

        is_any_job_running = False
        for job in run.jobs():
            if job.id == get_job_check_run_id():
                continue
            if job.status not in ("completed", "failure"):
                is_any_job_running = True
                break

        try:
            artifacts = list(run.get_artifacts())
            was_deployed = deploy_artifacts(artifacts)

            jobs = list(run.jobs())
            new_jobs_status = get_build_jobs_status(jobs)
            status_changed = False
            if new_jobs_status != jobs_status:
                jobs_status = new_jobs_status
                status_changed = True

            if was_deployed or status_changed:
                print("Updating build queue status...")
                pkgs = get_buildqueue_with_status(full_details=True)
                if not dry_run:
                    update_status(pkgs)
        except Exception:
            traceback.print_exc()
            print("Error while supervising, will retry in 5 minutes...")
            time.sleep(300)

        if is_any_job_running:
            print("Build jobs are still running, checking again in 30 seconds...")
            time.sleep(30)
        else:
            print("Build jobs are completed, stopping supervision.")
            break


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "supervise", help="Supervise build jobs", allow_abbrev=False)
    sub.add_argument(
        "--workflow-run-id",
        type=int,
        help="Workflow run to supervise, if not specified uses GITHUB_RUN_ID env var")
    sub.add_argument(
        "--dry-run", action="store_true", help="Only show what is going to be uploaded")
    sub.set_defaults(func=supervise)
