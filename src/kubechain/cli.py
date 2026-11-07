"""Command line for kubechain."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kubechain import __version__
from kubechain.catalog import ProfileError, catalog, load_profile
from kubechain.check import evaluate
from kubechain.live import LiveError, collect
from kubechain.load import LoadError, load_dir
from kubechain.model import exit_code
from kubechain.render import render_brief, render_json, render_markdown, render_text


class UsageError(Exception):
    """The command line does not name one source."""


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"kubechain {__version__}")
        return 0
    if not args.command:
        parser.print_help(sys.stderr)
        return 3
    try:
        return args.func(args)
    except (LoadError, ProfileError, LiveError, UsageError) as exc:
        print(exc, file=sys.stderr)
        return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubechain",
        description="Decide whether a blockchain workload on Kubernetes is production ready.",
    )
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")

    check = commands.add_parser("check", help="Report production gaps in a chain workload")
    _source_flags(check)
    check.add_argument("--brief", action="store_true", help="Print one line: namespace and verdict")
    check.set_defaults(func=cmd_check)

    report = commands.add_parser("report", help="Write a markdown report")
    _source_flags(report)
    report.add_argument("--output", type=Path, default=Path("kubechain-report.md"))
    report.set_defaults(func=cmd_report, brief=False)
    return parser


def _source_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifests", type=Path, help="Directory of YAML or JSON manifests")
    parser.add_argument("--namespace", help="Namespace to read from the current kubeconfig")
    parser.add_argument("--context", help="Kubeconfig context for a live read")
    parser.add_argument("--profile", type=Path, help="Local JSON profile, used when the container name matches comm")
    parser.add_argument("--json", action="store_true")


def cmd_check(args: argparse.Namespace) -> int:
    report = _report(args)
    if args.json:
        print(render_json(report), end="")
    elif args.brief:
        print(render_brief(report), end="")
    else:
        print(render_text(report), end="")
    return exit_code(report)


def cmd_report(args: argparse.Namespace) -> int:
    if args.output.is_dir():
        raise UsageError(f"output is a directory: {args.output}")
    report = _report(args)
    args.output.write_text(render_markdown(report))
    if args.json:
        print(render_json(report), end="")
    return exit_code(report)


def _report(args: argparse.Namespace):
    if bool(args.manifests) == bool(args.namespace):
        raise UsageError("pass either --manifests or --namespace")
    if args.manifests and args.context:
        raise UsageError("pass either --manifests or --namespace")
    overlay = load_profile(args.profile) if args.profile else None
    profiles = catalog()
    if args.manifests:
        objects = load_dir(args.manifests)
        return evaluate(objects, profiles, overlay, live=False, usage=None, classes_known=True)
    objects, usage, classes_known = collect(args.namespace, args.context)
    return evaluate(objects, profiles, overlay, live=True, usage=usage, classes_known=classes_known)
