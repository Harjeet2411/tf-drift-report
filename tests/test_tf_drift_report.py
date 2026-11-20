"""tf-drift-report tests: python3 -m unittest discover -s tests (no Terraform needed, synthetic plans only)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from tf_drift_report import cli, model, render  # noqa: E402

EXAMPLES = HERE.parent / "examples"


def rc(address, rtype, actions, before=None, after=None, **change):
    return {"address": address, "type": rtype,
            "change": {"actions": actions, "before": before, "after": after, "after_unknown": {},
                       "before_sensitive": {}, "after_sensitive": {}, **change}}


def doc(changes=(), drift=()):
    return json.dumps({"format_version": "1.2", "resource_changes": list(changes), "resource_drift": list(drift)})


class ActionTest(unittest.TestCase):
    def test_both_orders_of_delete_and_create_are_a_replacement(self):
        self.assertEqual(model.normalise_action(["delete", "create"]), "replace")
        self.assertEqual(model.normalise_action(["create", "delete"]), "replace")

    def test_single_actions(self):
        for action in ("create", "update", "delete", "read", "no-op"):
            self.assertEqual(model.normalise_action([action]), action)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(model.PlanError):
            model.normalise_action(["explode"])


class FlattenTest(unittest.TestCase):
    def test_nested_values_get_dotted_and_indexed_paths(self):
        flat = model.flatten({"a": {"b": 1}, "c": [{"d": 2}, 3], "e": []})
        self.assertEqual({k: v[0] for k, v in flat.items()}, {"a.b": 1, "c[0].d": 2, "c[1]": 3, "e": []})

    def test_sensitive_marks_follow_the_structure(self):
        flat = model.flatten({"user": "x", "password": "p"}, {"password": True})
        self.assertEqual((flat["user"][1], flat["password"][1]), (False, True))

    def test_a_wholly_sensitive_value_is_one_leaf(self):
        self.assertEqual(model.flatten({"k": "v"}, True), {"": ({"k": "v"}, True)})


class AttrChangeTest(unittest.TestCase):
    def test_only_differences_are_listed(self):
        attrs = model.attr_changes({"before": {"a": 1, "b": 2}, "after": {"a": 1, "b": 3}})
        self.assertEqual([(a.path, a.old, a.new) for a in attrs], [("b", 2, 3)])

    def test_added_and_removed_attributes(self):
        attrs = {a.path: a for a in model.attr_changes({"before": {"gone": 1}, "after": {"new": 2}})}
        self.assertFalse(attrs["gone"].has_new)
        self.assertFalse(attrs["new"].has_old)

    def test_unknown_values_are_flagged(self):
        [a] = model.attr_changes({"before": {"id": "i-1"}, "after": {}, "after_unknown": {"id": True}})
        self.assertTrue(a.unknown)

    def test_replace_paths_mark_the_forcing_attribute(self):
        attrs = {a.path: a for a in model.attr_changes(
            {"before": {"engine": "a", "size": 1}, "after": {"engine": "b", "size": 2}}, [["engine"]])}
        self.assertTrue(attrs["engine"].forces_replacement)
        self.assertFalse(attrs["size"].forces_replacement)


class LoadTest(unittest.TestCase):
    def test_document_splits_drift_from_planned_changes_and_skips_no_ops(self):
        plan = model.load(doc(
            changes=[rc("aws_instance.a", "aws_instance", ["update"], {"t": 1}, {"t": 2}),
                     rc("aws_iam_role.r", "aws_iam_role", ["no-op"], {"n": 1}, {"n": 1})],
            drift=[rc("aws_sg.s", "aws_security_group", ["update"], {"p": 1}, {"p": 2})]))
        self.assertEqual([c.address for c in plan.changes], ["aws_instance.a"])
        self.assertEqual([(c.address, c.drift) for c in plan.drift], [("aws_sg.s", True)])
        self.assertTrue(plan.detailed)

    def test_stream_is_read_without_attribute_values(self):
        plan = model.load((EXAMPLES / "plan-stream.jsonl").read_text())
        self.assertFalse(plan.detailed)
        self.assertEqual([(c.address, c.action) for c in plan.changes],
                         [("aws_db_instance.main", "replace"), ("aws_s3_bucket.logs", "delete")])
        self.assertEqual(len(plan.drift), 1)

    def test_errors(self):
        for bad, needle in (("", "empty"), ("not json", "not JSON"), ('{"hello": 1}', "not a Terraform plan"),
                            ('[1, 2]', "not a Terraform plan")):
            with self.assertRaisesRegex(model.PlanError, needle):
                model.load(bad)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.plan = model.load((EXAMPLES / "plan.json").read_text())

    def test_summary_line_counts_and_pluralises(self):
        self.assertEqual(render.summary_line(self.plan),
                         "Plan: 1 to add, 2 to change, 1 to replace, 1 to destroy. Drift: 2 resources changed outside Terraform.")
        self.assertEqual(render.summary_line(model.Plan()), "Plan: no changes. Drift: no drift.")
        one = model.load(doc(drift=[rc("a.b", "t", ["update"], {"x": 1}, {"x": 2})]))
        self.assertIn("1 resource changed", render.summary_line(one))

    def test_data_holding_resources_come_first_in_the_attention_list(self):
        plan = model.load(doc(changes=[rc("aws_instance.web", "aws_instance", ["delete"], {"i": 1}),
                                       rc("aws_db_instance.db", "aws_db_instance", ["delete"], {"i": 1})]))
        self.assertEqual([c.address for c in render.risky(plan)], ["aws_db_instance.db", "aws_instance.web"])

    def test_holds_data(self):
        self.assertTrue(render.holds_data("aws_s3_bucket"))
        self.assertFalse(render.holds_data("aws_instance"))

    def test_text_shows_old_and_new_values_and_the_forcing_attribute(self):
        out = render.text(self.plan)
        self.assertIn('ingress[0].cidr_blocks[0]: "10.0.0.0/8" -> "0.0.0.0/0"', out)
        self.assertIn('engine_version: "15.4" -> "16.1"  # forces replacement', out)
        self.assertIn("id: \"db-1\" -> (known after apply)", out)
        self.assertIn("arn: (known after apply)\n", out)

    def test_sensitive_values_never_appear(self):
        change = rc("aws_db_instance.d", "aws_db_instance", ["update"], {"password": "old-secret"},
                    {"password": "new-secret"}, before_sensitive={"password": True}, after_sensitive={"password": True})
        plan = model.load(doc(changes=[change]))
        for out in (render.text(plan), render.markdown(plan), render.as_json(plan)):
            self.assertNotIn("old-secret", out)
            self.assertNotIn("new-secret", out)
        self.assertIn("password: (sensitive value) changed", render.text(plan))

    def test_max_attrs_truncates_with_a_count(self):
        before = {f"k{i}": i for i in range(10)}
        after = {f"k{i}": i + 1 for i in range(10)}
        plan = model.load(doc(changes=[rc("a.b", "t", ["update"], before, after)]))
        out = render.text(plan, max_attrs=3)
        self.assertIn("... and 7 more attribute(s)", out)
        self.assertNotIn("k9", out)
        self.assertIn("k9", render.text(plan, max_attrs=-1))

    def test_markdown_has_a_warning_collapsible_sections_and_diff_blocks(self):
        out = render.markdown(self.plan, title="Prod plan")
        self.assertTrue(out.startswith("### Prod plan\n"))
        self.assertIn("2 resource(s) will be destroyed or replaced", out)
        self.assertIn("<details><summary>`aws_security_group.web` update</summary>", out)
        self.assertIn('- ingress[0].cidr_blocks[0]: "10.0.0.0/8"\n+ ingress[0].cidr_blocks[0]: "0.0.0.0/0"', out)

    def test_json_output_is_valid_and_has_counts(self):
        data = json.loads(render.as_json(self.plan))
        self.assertEqual(data["counts"], {"create": 1, "update": 2, "replace": 1, "delete": 1})
        self.assertEqual(len(data["drift"]), 2)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    PLAN = str(EXAMPLES / "plan.json")

    def test_report_exits_zero_without_fail_on(self):
        code, out, _ = run_cli(self.PLAN)
        self.assertEqual(code, 0)
        self.assertIn("Terraform drift report", out)

    def test_fail_on_drift_and_destroy_exit_2_and_say_why(self):
        code, _, err = run_cli(self.PLAN, "--fail-on", "drift,destroy")
        self.assertEqual(code, 2)
        self.assertIn("2 resource(s) drifted", err)
        self.assertIn("1 resource(s) would be destroyed", err)

    def test_fail_on_that_is_not_met_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(doc(changes=[rc("a.b", "t", ["create"], None, {"x": 1})]))
            self.assertEqual(run_cli(str(path), "--fail-on", "drift,destroy,replace")[0], 0)
            self.assertEqual(run_cli(str(path), "--fail-on", "change")[0], 2)

    def test_unknown_fail_on_value_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main([self.PLAN, "--fail-on", "everything"])
        self.assertEqual(cm.exception.code, 2)

    def test_missing_file_and_bad_input_exit_1(self):
        code, _, err = run_cli("/nonexistent/plan.json")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{}")
            code, _, err = run_cli(str(path))
        self.assertEqual(code, 1)
        self.assertIn("not a Terraform plan", err)

    def test_format_flag(self):
        self.assertTrue(run_cli(self.PLAN, "--format", "markdown")[1].startswith("### "))
        json.loads(run_cli(self.PLAN, "--format", "json")[1])


if __name__ == "__main__":
    unittest.main()
