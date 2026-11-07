# AWS_MCP_USAGE.md — how to look at AWS from this repo

For any agent (human or model) working in this checkout that needs to read
or change something in the station's AWS account. Written 2026-11-07 after a
local agent spent its budget fighting the `aws-mcp` server. Read this before
touching AWS. Infrastructure itself is defined in
`~/projects/terraform-aws-pilgrim/modules/ec2` (see its `AGENTS.md`); this
file is only about *access*.

## 0. Decide first: do you need AWS at all?

Usually not. Planning documents in this repo (SHEEP_PEN.md §0.4,
SCALABILITY.md, docs/backends.md §9, §12) record the deployed state with
dates. Trust the dated record for planning. Call AWS only to confirm a fact
before changing it, or to inspect a live failure.

## 1. Three ways in, from easiest to hardest

| Way | When | Identity it uses |
|-----|------|------------------|
| **AWS CLI in the shell** | Almost always. One-liners, read-only checks, log tailing | `[default]` = the operator's `aws login` session (admin), or `--profile mac-studio` (the station's own IAM user) |
| **boto3 in `.venv`** | Scripts that belong in `pilgrim/tools/` | Only `--profile mac-studio` works today (the `aws login` provider needs an extra package boto3 does not have here) |
| **`aws-mcp` MCP server** | When the harness gives you no shell, or you want the AWS docs/skills tools | Same `[default]` login session, via a SigV4 proxy |

### 1.1 AWS CLI

Installed by Homebrew: `/opt/homebrew/bin/aws` (v2.39). If `aws` is "command
not found", your shell PATH lacks `/opt/homebrew/bin`; call it by full path.

```sh
/opt/homebrew/bin/aws sts get-caller-identity                 # who am I
/opt/homebrew/bin/aws --profile mac-studio sts get-caller-identity
/opt/homebrew/bin/aws --region us-west-2 ecs describe-services \
    --cluster pg-ecs --services pg-app --query 'services[0].{running:runningCount,desired:desiredCount,td:taskDefinition}'
/opt/homebrew/bin/aws --region us-west-2 logs tail /aws/ecs/pg-app/app --since 10m
```

Facts about the profiles on the Mac (`~/.aws/config`):

- `[default]`: `login_session` from `aws login`, region **us-west-1**. It is
  the operator's admin role (`AccountFullAccessRole`) and **expires**. When
  calls fail with an expired-token or missing-credentials error, ask the
  operator to run `aws login` (in Claude Code: type `! aws login`).
  Always pass `--region us-west-2`; production lives there, not in the
  profile's default region.
- `[mac-studio]`: static keys, region us-west-2. This is what the station
  uses (`media.aws_profile`). Permissions: S3 read/write on the two
  `pilgrim-dot-farm-*-2` buckets, `GetSecretValue` on the RDS secret, and
  `logs:*`. It **cannot** read IAM, ECS, ElastiCache or RDS metadata; use it
  only to act as the station would.

### 1.2 boto3 from `.venv`

```sh
.venv/bin/python -c "import boto3; print(boto3.Session(profile_name='mac-studio').client('sts').get_caller_identity()['Arn'])"
```

`boto3.Session()` with no profile fails (`MissingDependencyException`: the
login credential provider needs an extra dependency). Pass
`profile_name="mac-studio"` and accept its narrow permissions, or use the
CLI. Follow `pilgrim/media_publish.py` and `pilgrim/db.py` for how the
station builds clients from `cfg.media.aws_profile`.

### 1.3 The `aws-mcp` server

Configured in `.mcp.json` (project scope) and in `~/.claude.json` (user
scope); the same entry in both:

```json
"aws-mcp": {
  "command": "uvx",
  "args": ["mcp-proxy-for-aws@latest", "https://aws-mcp.us-east-1.api.aws/mcp",
           "--metadata", "INSTALL_SOURCE=aws-cli"]
}
```

`mcp-proxy-for-aws` runs locally, signs each MCP request with SigV4 from the
default credential chain (the `[default]` login session), and forwards it to
AWS's hosted MCP endpoint. Useful flags if you change the entry:
`--region us-west-2`, `--profile default mac-studio` (per-call profile
switching), `--read-only` (hides tools that may write). No secrets belong in
`.mcp.json` (AGENTS hard rule 7); the entry above has none.

Tools it exposes:

| Tool | What it does | Notes |
|------|--------------|-------|
| `aws___run_script` | Runs **your Python** in a sandbox **on AWS's side** | The only way to call APIs. See §2 |
| `aws___get_presigned_url` | S3 upload/download URL for a local file | For moving files, never for secrets |
| `aws___get_tasks` | Poll long-running tasks | |
| `aws___search_documentation` / `aws___read_documentation` | AWS docs search and fetch | Parameter is `search_phrase`, not `query` |
| `aws___retrieve_skill` | Loads an AWS skill | |
| `aws___list_regions` / `aws___get_regional_availability` | Region info | |

## 2. `run_script`: the contract that trips agents up

The script does **not** run on this Mac. It runs in a remote sandbox that
has:

- **no `boto3`, no `botocore`, no `aws` CLI, no `pip`, no network**;
- one injected coroutine, `call_boto3`, which is the only way to reach AWS;
- these modules pre-imported: `asyncio, collections, csv, dataclasses,
  datetime, decimal, enum, fractions, functools, itertools, json, math, re,
  statistics, string, time, typing, uuid`, plus `ClientError`, `StringIO`,
  `BytesIO`;
- a helper library `aws_mcp` (`import aws_mcp; help(aws_mcp)`).

```python
async def call_boto3(*, service_name: str, operation_name: str,
                     region_name: str | None = None,
                     params: dict | None = None) -> dict: ...
```

Rules:

1. `operation_name` is the **PascalCase API name** (`ListUserPolicies`,
   `DescribeServices`, `GetSecretValue`), not the snake_case boto3 method.
2. Always pass `region_name="us-west-2"` for regional services. IAM and STS
   are global.
3. Results are auto-paginated; a `NextToken`/`Marker` in the result means
   there was more than the 1000 cap.
4. Put what you want back in a variable named `result`, and make `result`
   the **last expression** of the script.
5. Reads: `asyncio.gather(..., return_exceptions=True)`. Writes: let them
   raise.
6. Check `api_calls` in the response before trusting `return_value`; a
   script that never called an API returns an empty-looking success.
7. Never print or return secret values (`GetSecretValue` payloads, keys).
   Return ARNs, names and booleans.

Minimal working example:

```python
r = await call_boto3(service_name="iam", operation_name="ListUserPolicies",
                     params={"UserName": "mac-studio"})
result = {"inline_policies": r["PolicyNames"]}
result
```

Several reads at once:

```python
async def main():
    svc, logs = await asyncio.gather(
        call_boto3(service_name="ecs", operation_name="DescribeServices",
                   region_name="us-west-2",
                   params={"cluster": "pg-ecs", "services": ["pg-app"]}),
        call_boto3(service_name="logs", operation_name="DescribeLogGroups",
                   region_name="us-west-2",
                   params={"logGroupNamePrefix": "/aws/ecs/pg-"}),
        return_exceptions=True)
    return {"service": None if isinstance(svc, Exception) else
                {k: svc["services"][0].get(k) for k in ("runningCount", "desiredCount", "taskDefinition")},
            "log_groups": None if isinstance(logs, Exception) else
                [(g["logGroupName"], g.get("retentionInDays")) for g in logs["logGroups"]]}

result = await main()
result
```

### 2.1 Symptoms and what they mean

| You see | Cause | Do instead |
|---------|-------|-----------|
| `ModuleNotFoundError: boto3` | You wrote `import boto3` inside `run_script` | Use `call_boto3` |
| `aws: command not found` inside `run_script` | You tried `subprocess`/`os.system` in the sandbox | Use `call_boto3`, or run the CLI from your **own** shell |
| `aws: command not found` in your shell | PATH lacks Homebrew | `/opt/homebrew/bin/aws …` |
| `Unknown operation list_users` | snake_case name | `ListUsers` |
| `ExpiredToken`, `Unable to locate credentials` | `aws login` session lapsed | Operator runs `aws login`; do not retry in a loop |
| `AccessDenied` as `user/mac-studio` | You used the station's profile for an admin read | Use the default (login) identity |
| `ValidationException: Missing required field(s): 'search_phrase'` | Docs tool called with `query` | Use `search_phrase` |
| Script succeeds but `return_value` is `{}` | Forgot the trailing `result` expression | End the script with `result` |

Do not loop on any of these. Two failures of the same kind mean stop,
report the exact error, and switch to the CLI.

## 3. What you are allowed to do

- Reads are fine at any time with either identity.
- **Writes change production.** Terraform owns the infrastructure
  (`~/projects/terraform-aws-pilgrim`); change it there and `terraform
  apply`, not through ad-hoc API calls, so state does not drift. The one
  routine exception the operator has authorized is the image release path
  (build/push `pilgrimdotfarm/front-end`, bump `ecs_release`, apply).
- Never delete or overwrite S3 objects under `audio/` or `art/` (they are
  the delivered library; AGENTS hard rule 5 in spirit), never touch RDS
  snapshots or the Valkey cache through the API, never change IAM by hand.
- Never log, print, commit or paste credentials, `client_secret`s, or
  Secrets Manager values. Returning an ARN is fine.
- Prefer the AWS MCP docs tools (`search_documentation`,
  `read_documentation`) over memory for service limits and supported
  commands; cite the URL in `docs/backends.md` when you rely on one.

## 4. Names you will need (account 477688903218, us-west-2)

| Thing | Name |
|-------|------|
| ECS cluster / service | `pg-ecs` / `pg-app` |
| Task role / execution role | `pg-app-tasks-*` / `pg-app-task-exec-*` (module-generated suffixes) |
| Container log group | `/aws/ecs/pg-app/app` (7-day retention, set in `ecs.tf`) |
| ALB / target group | `pg-alb` / `pg-http` |
| Valkey | serverless cache `pg-cache` (TLS, no AUTH; `REDIS_URL` in `.env`) |
| RDS secret | the `rds!db-…` secret named by `DB_PASS_SECRET_ARN` |
| Stripe secret | `pg-stripe` (`STRIPE_SECRET_ARN`) |
| Buckets | `pilgrim-dot-farm-public-2`, `pilgrim-dot-farm-private-2` |
| CloudFront | `E2J4EQA5VW0HUG` |
| IAM users | `mac-studio` (the station), `kenny-logger` (`logs:*`, not used by the station) |
| Bastion EC2 | `pg` (instance role `pg-s3-read`; also the Tailscale subnet router) |

Verify a name before acting on it; this table is a dated snapshot
(2026-11-07), not the source of truth.
