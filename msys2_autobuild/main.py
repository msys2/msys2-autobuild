import argparse
import sys
import logging
from typing import List

from . import (cmd_build, cmd_clean_assets, cmd_clear_failed, cmd_fetch_assets,
               cmd_show_build, cmd_update_status, cmd_upload_assets,
               cmd_write_build_plan)
from .utils import install_requests_cache


def main(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(description="Build packages", allow_abbrev=False)
    parser.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        help='Increase verbosity (can be used multiple times)'
    )
    parser.set_defaults(func=lambda *x: parser.print_help())
    subparsers = parser.add_subparsers(title="subcommands")
    cmd_build.add_parser(subparsers)
    cmd_show_build.add_parser(subparsers)
    cmd_write_build_plan.add_parser(subparsers)
    cmd_update_status.add_parser(subparsers)
    cmd_fetch_assets.add_parser(subparsers)
    cmd_upload_assets.add_parser(subparsers)
    cmd_clear_failed.add_parser(subparsers)
    cmd_clean_assets.add_parser(subparsers)

    args = parser.parse_args(argv[1:])
    level_map = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
    logging.basicConfig(
        level=level_map.get(args.verbose, logging.DEBUG),
        handlers=[logging.StreamHandler(sys.stderr)],
        format='[%(asctime)s] [%(levelname)8s] [%(name)s:%(module)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    with install_requests_cache():
        args.func(args)


def run() -> None:
    return main(sys.argv)
