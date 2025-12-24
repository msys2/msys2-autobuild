from typing import Any
import json

from .queue import get_buildqueue_with_status, get_status


def run_show_status(args: Any) -> None:
    status_object = get_status(get_buildqueue_with_status(full_details=True))
    print(json.dumps(status_object, indent=2))


def add_parser(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "show-status", help="Show the status", allow_abbrev=False)
    sub.set_defaults(func=run_show_status)
