"""Turn a Plan into text, Markdown (for PR comments) or JSON."""
from __future__ import annotations

import json

from .model import SENSITIVE, UNKNOWN, Change, Plan

SYMBOL = {"create": "+", "update": "~", "replace": "-/+", "delete": "-", "read": "<=", "no-op": " "}
VERB = {"create": "add", "update": "change", "replace": "replace", "delete": "destroy", "read": "read"}
# Types whose loss means lost data, not just a restart.
DATA_HINTS = ("db_instance", "rds_cluster", "bucket", "dynamodb_table", "ebs_volume", "efs_file_system",
              "kms_key", "sql_database", "elasticache", "redshift", "s3_", "storage_account", "key_vault",
              "disk", "mongodb", "postgresql", "mysql", "database")


def holds_data(res_type: str) -> bool:
    t = res_type.lower()
    return any(h in t for h in DATA_HINTS)


def counts(plan: Plan) -> dict:
    out = {a: 0 for a in ("create", "update", "replace", "delete")}
    for c in plan.changes:
        if c.action in out:
            out[c.action] += 1
    return out


def risky(plan: Plan) -> list:
    """Planned deletions and replacements, data-holding resources first."""
    hits = [c for c in plan.changes if c.action in ("delete", "replace")]
    return sorted(hits, key=lambda c: (not holds_data(c.type), c.address))


def summary_line(plan: Plan) -> str:
    n = counts(plan)
    parts = [f"{n[a]} to {VERB[a]}" for a in ("create", "update", "replace", "delete") if n[a]]
    plan_part = ", ".join(parts) if parts else "no changes"
    drift = len(plan.drift)
    drift_part = f"{drift} resource{'s' if drift != 1 else ''} changed outside Terraform" if drift else "no drift"
    return f"Plan: {plan_part}. Drift: {drift_part}."


def _show(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def attr_sides(a):
    """(old, new) as display strings, None for the side that does not exist."""
    if a.sensitive:
        return (SENSITIVE if a.has_old else None), (SENSITIVE if a.has_new or a.unknown else None)
    old = _show(a.old) if a.has_old else None
    new = UNKNOWN if a.unknown else (_show(a.new) if a.has_new else None)
    return old, new


def attr_line(a) -> str:
    old, new = attr_sides(a)
    if old is None:
        text = f"{a.path}: {new}" if a.unknown else f"{a.path}: {new} (added)"
    elif new is None:
        text = f"{a.path}: {old} (removed)"
    elif a.sensitive:
        text = f"{a.path}: {SENSITIVE} changed"
    else:
        text = f"{a.path}: {old} -> {new}"
    return text + ("  # forces replacement" if a.forces_replacement else "")


def diff_lines(a) -> list:
    """Lines for a Markdown diff block: the old value with '-', the new one with '+'."""
    old, new = attr_sides(a)
    note = "  # forces replacement" if a.forces_replacement else ""
    lines = []
    if old is not None:
        lines.append(f"- {a.path}: {old}")
    if new is not None:
        lines.append(f"+ {a.path}: {new}{note}")
    return lines


def _attr_lines(change: Change, max_attrs: int) -> list:
    shown = change.attrs[:max_attrs] if max_attrs >= 0 else change.attrs
    lines = [attr_line(a) for a in shown]
    if len(shown) < len(change.attrs):
        lines.append(f"... and {len(change.attrs) - len(shown)} more attribute(s)")
    return lines


def _headline(c: Change) -> str:
    extra = " (holds data)" if c.action in ("delete", "replace") and holds_data(c.type) else ""
    reason = f" [{c.reason}]" if c.reason else ""
    return f"{SYMBOL.get(c.action, '?')} {c.address}  {c.action}{extra}{reason}"


def text(plan: Plan, max_attrs: int = 20) -> str:
    out = ["Terraform drift report", "", summary_line(plan)]
    if not plan.detailed:
        out += ["(input is the terraform plan -json stream: no attribute values. Use terraform show -json for diffs.)"]
    if plan.drift:
        out += ["", f"Drift: changed outside Terraform ({len(plan.drift)})"]
        for c in plan.drift:
            out.append("  " + _headline(c))
            out += ["      " + line for line in _attr_lines(c, max_attrs)]
    hits = risky(plan)
    if hits:
        out += ["", f"Needs attention: destroyed or replaced ({len(hits)})"]
        out += ["  " + _headline(c) for c in hits]
    rest = [c for c in plan.changes if c.action != "no-op"]
    if rest:
        out += ["", f"Planned changes ({len(rest)})"]
        for c in rest:
            out.append("  " + _headline(c))
            out += ["      " + line for line in _attr_lines(c, max_attrs)]
    return "\n".join(out) + "\n"


def markdown(plan: Plan, max_attrs: int = 20, title: str = "Terraform drift report") -> str:
    out = [f"### {title}", "", f"**{summary_line(plan)}**"]
    if not plan.detailed:
        out += ["", "_Input is the `terraform plan -json` stream, so there are no attribute values. "
                    "Use `terraform show -json` for diffs._"]
    hits = risky(plan)
    if hits:
        out += ["", f"> :warning: **{len(hits)} resource(s) will be destroyed or replaced**"]
        out += [f"> - `{c.address}` ({c.action}{', holds data' if holds_data(c.type) else ''})" for c in hits]

    def section(heading: str, items: list) -> None:
        if not items:
            return
        out.extend(["", f"#### {heading} ({len(items)})", ""])
        for c in items:
            shown = c.attrs[:max_attrs] if max_attrs >= 0 else c.attrs
            lines = [ln for a in shown for ln in diff_lines(a)]
            if len(shown) < len(c.attrs):
                lines.append(f"  ... and {len(c.attrs) - len(shown)} more attribute(s)")
            label = f"`{c.address}` {c.action}" + (f" ({c.reason})" if c.reason else "")
            if lines:
                out.extend([f"<details><summary>{label}</summary>", "", "```diff"])
                out.extend(lines)
                out.extend(["```", "</details>"])
            else:
                out.append(f"- {label}")

    section("Drift: changed outside Terraform", plan.drift)
    section("Planned changes", [c for c in plan.changes if c.action != "no-op"])
    return "\n".join(out) + "\n"


def as_json(plan: Plan) -> str:
    def one(c: Change) -> dict:
        return {"address": c.address, "type": c.type, "action": c.action, "reason": c.reason,
                "holds_data": holds_data(c.type),
                "attributes": [{"path": a.path, "old": None if a.sensitive else a.old,
                                "new": None if a.sensitive or a.unknown else a.new, "sensitive": a.sensitive,
                                "known_after_apply": a.unknown, "forces_replacement": a.forces_replacement}
                               for a in c.attrs]}

    return json.dumps({"summary": summary_line(plan), "counts": counts(plan), "drift": [one(c) for c in plan.drift],
                       "changes": [one(c) for c in plan.changes if c.action != "no-op"]}, indent=2) + "\n"
