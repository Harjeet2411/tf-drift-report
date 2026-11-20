"""Read Terraform plan JSON into a list of changes.

Two inputs are understood:

* the document from ``terraform show -json tfplan``: has ``resource_changes`` (what apply would do) and
  ``resource_drift`` (what changed outside Terraform since the last apply), both with before/after values;
* the JSON Lines stream from ``terraform plan -json``: has ``planned_change`` and ``resource_drift`` messages that
  carry the address and the action only, so no attribute diff is available.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

ACTIONS = ("create", "update", "replace", "delete", "read", "no-op")
UNKNOWN = "(known after apply)"
SENSITIVE = "(sensitive value)"


class PlanError(ValueError):
    """The input is not a Terraform plan this tool understands."""


@dataclass
class AttrChange:
    path: str
    old: object = None
    new: object = None
    has_old: bool = True
    has_new: bool = True
    sensitive: bool = False
    unknown: bool = False
    forces_replacement: bool = False


@dataclass
class Change:
    address: str
    type: str
    action: str
    drift: bool = False
    reason: str = ""
    attrs: list = field(default_factory=list)


@dataclass
class Plan:
    drift: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    detailed: bool = True  # False for the JSON Lines stream, which has no before/after values


def normalise_action(actions) -> str:
    """["delete", "create"] (either order) is a replacement; a single action maps to itself."""
    actions = [actions] if isinstance(actions, str) else list(actions or [])
    if set(actions) == {"delete", "create"}:
        return "replace"
    if len(actions) == 1 and actions[0] in ACTIONS:
        return actions[0]
    if len(actions) == 1 and actions[0] == "forget":
        return "delete"
    raise PlanError(f"unknown action list {actions!r}")


def _join(prefix: str, key) -> str:
    if isinstance(key, int):
        return f"{prefix}[{key}]"
    return f"{prefix}.{key}" if prefix else str(key)


def flatten(value, marks=None, prefix: str = "") -> dict:
    """{path: (leaf value, marked)}; `marks` is the parallel sensitive / unknown structure, True at the marked spots."""
    if marks is True:
        return {prefix: (value, True)}
    out: dict = {}
    if isinstance(value, dict) and value:
        for k, v in value.items():
            sub = marks.get(k) if isinstance(marks, dict) else None
            out.update(flatten(v, sub, _join(prefix, k)))
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            sub = marks[i] if isinstance(marks, list) and i < len(marks) else None
            out.update(flatten(v, sub, _join(prefix, i)))
    elif prefix or value not in ({}, []):  # an empty root has no attributes at all
        out[prefix] = (value, bool(marks))
    return out


def _marked_paths(marks, prefix: str = "") -> set:
    if marks is True:
        return {prefix}
    paths: set = set()
    if isinstance(marks, dict):
        for k, v in marks.items():
            paths |= _marked_paths(v, _join(prefix, k))
    elif isinstance(marks, list):
        for i, v in enumerate(marks):
            paths |= _marked_paths(v, _join(prefix, i))
    return paths


def _under(path: str, roots: set) -> bool:
    return any(path == r or path.startswith((r + ".", r + "[")) for r in roots)


def attr_changes(change: dict, replace_paths=()) -> list:
    """Attribute-level differences of one resource change, sensitive values masked later by the renderer."""
    before, after = change.get("before"), change.get("after")
    b = flatten(before, change.get("before_sensitive")) if before is not None else {}
    a = flatten(after, change.get("after_sensitive")) if after is not None else {}
    unknown = _marked_paths(change.get("after_unknown"))
    forcing = {".".join(str(p) for p in path) for path in replace_paths if all(isinstance(p, str) for p in path)}
    out = []
    for path in sorted(set(b) | set(a) | unknown):
        in_b, in_a = path in b, path in a
        is_unknown = path in unknown or _under(path, unknown)
        old, old_s = b.get(path, (None, False))
        new, new_s = a.get(path, (None, False))
        if in_b and in_a and old == new and not is_unknown:
            continue
        if not in_b and not in_a and not is_unknown:
            continue
        out.append(AttrChange(path, old, new, in_b, in_a, old_s or new_s, is_unknown,
                              bool(forcing) and _under(path, forcing)))
    return out


def _from_document(doc: dict) -> Plan:
    plan = Plan()
    for key, drift, target in (("resource_drift", True, plan.drift), ("resource_changes", False, plan.changes)):
        for rc in doc.get(key) or []:
            change = rc.get("change") or {}
            action = normalise_action(change.get("actions"))
            if not drift and action == "no-op":
                continue
            target.append(Change(
                address=rc.get("address", "?"), type=rc.get("type", ""), action=action, drift=drift,
                reason=rc.get("action_reason", ""),
                attrs=attr_changes(change, change.get("replace_paths") or []) if action in ("update", "replace") else [],
            ))
    return plan


def _from_stream(lines: list) -> Plan:
    plan = Plan(detailed=False)
    for msg in lines:
        kind = msg.get("type")
        if kind not in ("planned_change", "resource_drift"):
            continue
        change = msg.get("change") or {}
        res = change.get("resource") or {}
        action = change.get("action", "")
        if action in ("move", "import", "noop"):  # nothing to report
            continue
        action = "delete" if action == "forget" else action
        if action not in ACTIONS:
            raise PlanError(f"unknown action {action!r} in the plan stream")
        item = Change(address=res.get("addr", "?"), type=res.get("resource_type", ""), action=action,
                      drift=kind == "resource_drift", reason=change.get("reason", ""))
        (plan.drift if item.drift else plan.changes).append(item)
    return plan


def load(text: str) -> Plan:
    text = text.strip()
    if not text:
        raise PlanError("the input is empty")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise PlanError(f"the input is not JSON: {exc.msg} (line {exc.lineno})") from exc
        if not any(isinstance(m, dict) and m.get("type") for m in lines):
            raise PlanError("no Terraform messages found in the JSON Lines input")
        return _from_stream(lines)
    if isinstance(doc, dict) and ("resource_changes" in doc or "resource_drift" in doc or "format_version" in doc):
        return _from_document(doc)
    if isinstance(doc, dict) and doc.get("type"):  # a one-line stream
        return _from_stream([doc])
    raise PlanError("this is JSON but not a Terraform plan: expected resource_changes (terraform show -json)"
                    " or the terraform plan -json message stream")
