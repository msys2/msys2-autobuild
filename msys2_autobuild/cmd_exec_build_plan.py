import json
from typing import Any

from .gh import get_current_repo, create_dispatch, make_writable


def exec_build_plan(args: Any) -> None:
    target_file = args.target_file
    build_plan_file = args.build_plan_file
    branch = args.target_branch

    with open(build_plan_file, "rb") as h:
        build_plan = h.read().decode()

    repo = get_current_repo()
    workflow = repo.get_workflow("build-jobs.yml")
    with make_writable(workflow):
        workflow_run = create_dispatch(workflow, branch, inputs={"build_plan": build_plan})

    with open(target_file, "wb") as h:
        h.write(json.dumps({
            "run_id": workflow_run.id,
        }).encode())


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "exec-build-plan", help="Execute the build plan", allow_abbrev=False)
    sub.add_argument(
        "--target-branch", type=str, help="Branch to build in", required=True)
    sub.add_argument("build_plan_file")
    sub.add_argument("target_file")
    sub.set_defaults(func=exec_build_plan)
