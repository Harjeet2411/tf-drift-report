from __future__ import annotations

import argparse
import sys

from . import __version__, render
from .model import PlanError, load

FAIL_ON = ("drift", "destroy", "replace", "change")


def failures(plan, conditions) -> list:
    """Which of the --fail-on conditions the plan meets."""
    n = render.counts(plan)
    met = []
    if "drift" in conditions and plan.drift:
        met.append(f"{len(plan.drift)} resource(s) drifted")
    if "destroy" in conditions and n["delete"]:
        met.append(f"{n['delete']} resource(s) would be destroyed")
    if "replace" in conditions and n["replace"]:
        met.append(f"{n['replace']} resource(s) would be replaced")
    if "change" in conditions and sum(n.values()):
        met.append(f"{sum(n.values())} change(s) planned")
    return met


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tf-drift-report", description="Readable drift report from Terraform plan JSON.")
    p.add_argument("plan", nargs="?", default="-", help="plan JSON file (terraform show -json tfplan, or terraform "
                   "plan -json output); default: stdin")
    p.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    p.add_argument("--fail-on", default="", metavar="LIST",
                   help="comma list of drift, destroy, replace, change: exit 2 when any is met")
    p.add_argument("--max-attrs", type=int, default=20, help="attributes shown per resource (-1 for all; default 20)")
    p.add_argument("--title", default="Terraform drift report", help="heading of the markdown report")
    p.add_argument("--version", action="version", version=f"tf-drift-report {__version__}")
    args = p.parse_args(argv)

    conditions = [c.strip() for c in args.fail_on.split(",") if c.strip()]
    bad = [c for c in conditions if c not in FAIL_ON]
    if bad:
        p.error(f"unknown --fail-on value(s): {', '.join(bad)} (use {', '.join(FAIL_ON)})")

    try:
        raw = sys.stdin.read() if args.plan == "-" else open(args.plan, encoding="utf-8").read()
    except OSError as exc:
        print(f"tf-drift-report: cannot read {args.plan}: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        plan = load(raw)
    except PlanError as exc:
        print(f"tf-drift-report: {exc}", file=sys.stderr)
        return 1

    if args.format == "markdown":
        sys.stdout.write(render.markdown(plan, args.max_attrs, args.title))
    elif args.format == "json":
        sys.stdout.write(render.as_json(plan))
    else:
        sys.stdout.write(render.text(plan, args.max_attrs))
    met = failures(plan, conditions)
    for reason in met:
        print(f"tf-drift-report: failing on {reason}", file=sys.stderr)
    return 2 if met else 0
