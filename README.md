# tf-drift-report

[![CI](https://github.com/Harjeet2411/tf-drift-report/actions/workflows/ci.yml/badge.svg)](https://github.com/Harjeet2411/tf-drift-report/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![License](https://img.shields.io/badge/license-MIT-green)

**`terraform plan` is 600 lines of output and the one line that matters is somewhere in the middle.** A security group opened to `0.0.0.0/0` by hand in the console, a database that the plan quietly wants to replace, a bucket about to be destroyed. Reviewers skim past them.

`tf-drift-report` reads the plan as JSON and writes the short version: what **drifted** (changed outside Terraform since the last apply), what the plan will **change**, and a **needs attention** list of everything that will be destroyed or replaced, with data-holding resources (databases, buckets, volumes, keys) first. Output is plain text for the terminal or Markdown for a pull request comment. It has no dependencies, needs no cloud credentials, and never touches your state: it only reads a JSON file.

## Demo

Run in [`examples/`](examples), which holds a synthetic plan (no real infrastructure):

```console
$ terraform show -json tfplan > plan.json
$ tf-drift-report plan.json
Terraform drift report

Plan: 1 to add, 2 to change, 1 to replace, 1 to destroy. Drift: 2 resources changed outside Terraform.

Drift: changed outside Terraform (2)
  ~ aws_security_group.web  update
      ingress[0].cidr_blocks[0]: "10.0.0.0/8" -> "0.0.0.0/0"
      ingress[1].cidr_blocks[0]: "0.0.0.0/0" (added)
      ingress[1].from_port: 22 (added)
      ingress[1].protocol: "tcp" (added)
      ingress[1].to_port: 22 (added)
  ~ aws_instance.app[0]  update
      tags.owner: "platform" (removed)

Needs attention: destroyed or replaced (2)
  -/+ aws_db_instance.main  replace (holds data) [replace_because_cannot_update]
  - aws_s3_bucket.logs  delete (holds data)

Planned changes (5)
  ~ aws_security_group.web  update
      ingress[0].cidr_blocks[0]: "0.0.0.0/0" -> "10.0.0.0/8"
      ingress[1].cidr_blocks[0]: "0.0.0.0/0" (removed)
      ingress[1].from_port: 22 (removed)
      ingress[1].protocol: "tcp" (removed)
      ingress[1].to_port: 22 (removed)
  ~ aws_instance.app[0]  update
      tags.owner: "platform" (added)
  -/+ aws_db_instance.main  replace (holds data) [replace_because_cannot_update]
      arn: (known after apply)
      engine_version: "15.4" -> "16.1"  # forces replacement
      id: "db-1" -> (known after apply)
  - aws_s3_bucket.logs  delete (holds data)
  + aws_lb_target_group_attachment.app  create
```

Passwords and other values Terraform marks sensitive are never printed, only `(sensitive value) changed`.

### As a pull request comment

`--format markdown` gives collapsible sections and diff blocks (shown here cut to 3 attributes per resource):

```console
$ tf-drift-report plan.json --format markdown --max-attrs 3
### Terraform drift report

**Plan: 1 to add, 2 to change, 1 to replace, 1 to destroy. Drift: 2 resources changed outside Terraform.**

> :warning: **2 resource(s) will be destroyed or replaced**
> - `aws_db_instance.main` (replace, holds data)
> - `aws_s3_bucket.logs` (delete, holds data)

#### Drift: changed outside Terraform (2)

<details><summary>`aws_security_group.web` update</summary>

```diff
- ingress[0].cidr_blocks[0]: "10.0.0.0/8"
+ ingress[0].cidr_blocks[0]: "0.0.0.0/0"
+ ingress[1].cidr_blocks[0]: "0.0.0.0/0"
+ ingress[1].from_port: 22
  ... and 2 more attribute(s)
```
</details>
<details><summary>`aws_instance.app[0]` update</summary>

```diff
- tags.owner: "platform"
```
</details>

#### Planned changes (5)

<details><summary>`aws_security_group.web` update</summary>

```diff
- ingress[0].cidr_blocks[0]: "0.0.0.0/0"
+ ingress[0].cidr_blocks[0]: "10.0.0.0/8"
- ingress[1].cidr_blocks[0]: "0.0.0.0/0"
- ingress[1].from_port: 22
  ... and 2 more attribute(s)
```
</details>
<details><summary>`aws_instance.app[0]` update</summary>

```diff
+ tags.owner: "platform"
```
</details>
<details><summary>`aws_db_instance.main` replace (replace_because_cannot_update)</summary>

```diff
+ arn: (known after apply)
- engine_version: "15.4"
+ engine_version: "16.1"  # forces replacement
- id: "db-1"
+ id: (known after apply)
```
</details>
- `aws_s3_bucket.logs` delete
- `aws_lb_target_group_attachment.app` create
```

### Fail the build on what you do not want

```console
$ tf-drift-report plan.json --fail-on drift,destroy
...
Terraform drift report

Plan: 1 to add, 2 to change, 1 to replace, 1 to destroy. Drift: 2 resources changed outside Terraform.

Drift: changed outside Terraform (2)
  ~ aws_security_group.web  update
      ingress[0].cidr_blocks[0]: "10.0.0.0/8" -> "0.0.0.0/0"
      ingress[1].cidr_blocks[0]: "0.0.0.0/0" (added)
      ingress[1].from_port: 22 (added)
      ingress[1].protocol: "tcp" (added)
      ingress[1].to_port: 22 (added)
  ~ aws_instance.app[0]  update
      tags.owner: "platform" (removed)

Needs attention: destroyed or replaced (2)
  -/+ aws_db_instance.main  replace (holds data) [replace_because_cannot_update]
  - aws_s3_bucket.logs  delete (holds data)

Planned changes (5)
  ~ aws_security_group.web  update
      ingress[0].cidr_blocks[0]: "0.0.0.0/0" -> "10.0.0.0/8"
      ingress[1].cidr_blocks[0]: "0.0.0.0/0" (removed)
      ingress[1].from_port: 22 (removed)
      ingress[1].protocol: "tcp" (removed)
      ingress[1].to_port: 22 (removed)
  ~ aws_instance.app[0]  update
      tags.owner: "platform" (added)
  -/+ aws_db_instance.main  replace (holds data) [replace_because_cannot_update]
      arn: (known after apply)
      engine_version: "15.4" -> "16.1"  # forces replacement
      id: "db-1" -> (known after apply)
  - aws_s3_bucket.logs  delete (holds data)
  + aws_lb_target_group_attachment.app  create
tf-drift-report: failing on 2 resource(s) drifted
tf-drift-report: failing on 1 resource(s) would be destroyed
```

`--fail-on` takes any of `drift`, `destroy`, `replace`, `change` and exits with code `2` when one is met (`1` is a bad or missing input).

### The streaming format

`terraform plan -json` writes one JSON message per line. That stream carries the address and the action of each change but not the before and after values, so the report has no attribute diff for it:

```console
$ terraform plan -json | tf-drift-report
Terraform drift report

Plan: 1 to replace, 1 to destroy. Drift: 1 resource changed outside Terraform.
(input is the terraform plan -json stream: no attribute values. Use terraform show -json for diffs.)

Drift: changed outside Terraform (1)
  ~ aws_security_group.web  update

Needs attention: destroyed or replaced (2)
  -/+ aws_db_instance.main  replace (holds data) [cannot_update]
  - aws_s3_bucket.logs  delete (holds data)

Planned changes (2)
  -/+ aws_db_instance.main  replace (holds data) [cannot_update]
  - aws_s3_bucket.logs  delete (holds data)
```

## Install

```console
pip install git+https://github.com/Harjeet2411/tf-drift-report
```

or run it straight from a checkout with `python -m tf_drift_report plan.json`. Python 3.9 or newer.

## Use in GitHub Actions

```yaml
- run: terraform plan -out=tfplan
- run: terraform show -json tfplan > plan.json
- run: tf-drift-report plan.json --format markdown > report.md
- run: tf-drift-report plan.json --fail-on destroy   # block the merge on any destroy
- uses: marocchino/sticky-pull-request-comment@v2     # pin this to a SHA, see gha-pinner
  with:
    path: report.md
```

Drift only appears when Terraform refreshes state during the plan, so do not pass `-refresh=false`.

## Options

| Option | Meaning |
|---|---|
| `plan` | plan JSON file, or `-` for stdin (default) |
| `--format text\|markdown\|json` | output format (default `text`) |
| `--fail-on LIST` | comma list of `drift`, `destroy`, `replace`, `change`; exit 2 when any is met |
| `--max-attrs N` | attributes shown per resource, `-1` for all (default 20) |
| `--title TEXT` | heading of the Markdown report |

## Tests

```console
python -m unittest discover -s tests
```

27 tests run on Python 3.9, 3.11 and 3.13 in CI, using only synthetic plans.

## Roadmap

- Group identical drift across `count` / `for_each` instances
- Flag security-group and IAM changes that widen access, not only destroys
- Terraform Cloud and OpenTofu plan variants

## Licence

MIT
