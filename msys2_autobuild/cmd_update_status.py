from typing import Any

from .queue import get_buildqueue_with_status, update_status


def run_update_status(args: Any) -> None:
    update_status(get_buildqueue_with_status(full_details=True))


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "update-status", help="Update the status file", allow_abbrev=False)
    sub.set_defaults(func=run_update_status)
