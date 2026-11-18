"""usptrader-ops: set up and operate the deployed trader from a laptop (Windows, macOS or Linux).

    usptrader-ops setup-profile        once: an AWS profile whose keys refresh themselves
    usptrader-ops preflight            what the account's guardrails allow, before terraform apply
    usptrader-ops secrets [FILTER]     fill the SSM secrets (hidden prompt; --clipboard to skip pasting)
    usptrader-ops push-image           build the image and push it to ECR as :live
    usptrader-ops run premarket        run one job on Fargate now and print its logs
    usptrader-ops run resume --mode aggressive

Everything it needs (registry, network, platform) is looked up in AWS, so it works from any
folder and doesn't need Terraform. Uses the usual AWS settings: AWS_PROFILE (or --profile)
and the region (default ap-southeast-2).
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

NAME = "us-paper-trader"
DEFAULT_REGION = "ap-southeast-2"
PROFILE = "usptrader"


# ---- small helpers ----------------------------------------------------------------------------
class OpsError(RuntimeError):
    """A problem the user can fix; printed without a traceback."""


def say(msg: str = "", color: str = "") -> None:
    codes = {"green": "32", "red": "31", "yellow": "33", "cyan": "36"}
    if color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None:
        msg = f"\033[{codes[color]}m{msg}\033[0m"
    print(msg, flush=True)


def error_code(exc: Exception) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "") if exc else ""


DENIED = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedAccess")
EXPIRED = ("ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidClientTokenId")


def session(profile: str | None, region: str):
    import boto3

    return boto3.Session(profile_name=profile or None, region_name=region)


def check_signed_in(sess) -> str:
    """Account id, or a clear message about how to sign in."""
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        CredentialRetrievalError,
        NoCredentialsError,
        TokenRetrievalError,
    )

    try:
        return sess.client("sts").get_caller_identity()["Account"]
    except (NoCredentialsError, TokenRetrievalError, CredentialRetrievalError) as exc:
        raise OpsError(f"Not signed in to AWS ({exc}). Run: aws login --region {sess.region_name}") from exc
    except ClientError as exc:
        if error_code(exc) in EXPIRED:
            raise OpsError(
                "Your AWS keys have expired. Run 'aws login', or set up a self-refreshing profile once "
                "with 'usptrader-ops setup-profile'. If you exported keys into this terminal earlier, "
                "close it or clear AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN."
            ) from exc
        raise
    except BotoCoreError as exc:  # network, proxy, unknown profile name...
        raise OpsError(f"Couldn't reach AWS: {exc}") from exc


# ---- setup-profile ----------------------------------------------------------------------------
def setup_profile(
    source: str = "default", region: str = DEFAULT_REGION, config_path: Path | None = None
) -> Path:
    """Add [profile usptrader] whose keys are fetched fresh, via `aws login`, every time they're needed.

    Appends to ~/.aws/config without rewriting anything already there.
    """
    path = config_path or Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    parser = configparser.RawConfigParser()
    parser.read_string(existing)
    if parser.has_section(f"profile {PROFILE}"):
        return path
    block = (
        f"\n[profile {PROFILE}]\n"
        f"region = {region}\n"
        f"credential_process = aws configure export-credentials --profile {source} --format process\n"
    )
    with path.open("a", newline="\n") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(block)
    return path


# ---- preflight --------------------------------------------------------------------------------
@dataclass
class Check:
    label: str
    status: str  # OK | DENIED | ERROR: ...

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def _probe(label: str, fn) -> Check:
    try:
        fn()
        return Check(label, "OK")
    except Exception as exc:  # noqa: BLE001
        code = error_code(exc)
        if code == "DryRunOperation":
            return Check(label, "OK")
        if code in DENIED or "explicit deny" in str(exc) or "not authorized" in str(exc):
            return Check(label, "DENIED")
        return Check(label, f"ERROR: {code or type(exc).__name__}: {str(exc)[:90]}")


def preflight(sess) -> tuple[list[Check], list[str]]:
    ec2, iam = sess.client("ec2"), sess.client("iam")
    checks: list[Check] = []

    checks.append(
        _probe("Create own VPC (dry run)", lambda: ec2.create_vpc(CidrBlock="10.42.0.0/16", DryRun=True))
    )
    default_vpc: dict = {}

    def find_default_vpc():
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise RuntimeError("no default VPC in this region")
        default_vpc["id"] = vpcs[0]["VpcId"]

    checks.append(_probe("Default VPC exists", find_default_vpc))
    if default_vpc:
        checks.append(
            _probe(
                "Create security group (dry run)",
                lambda: ec2.create_security_group(
                    GroupName="usptrader-preflight",
                    Description="preflight",
                    VpcId=default_vpc["id"],
                    DryRun=True,
                ),
            )
        )

    # IAM has no dry run: create throwaway objects and delete them at once.
    trust = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )
    doc = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "logs:CreateLogStream", "Resource": "*"}],
        }
    )
    created: dict = {}

    def make_policy():
        created["policy"] = iam.create_policy(PolicyName=f"{NAME}-preflight", PolicyDocument=doc)["Policy"][
            "Arn"
        ]

    def make_bounded_role():
        iam.create_role(
            RoleName=f"{NAME}-preflight",
            AssumeRolePolicyDocument=trust,
            PermissionsBoundary=created["policy"],
        )
        created["role"] = f"{NAME}-preflight"

    def make_role():
        iam.create_role(RoleName=f"{NAME}-preflight-2", AssumeRolePolicyDocument=trust)
        created["role2"] = f"{NAME}-preflight-2"

    try:
        checks.append(_probe("Create managed IAM policy", make_policy))
        if "policy" in created:
            checks.append(_probe("Role with permissions boundary", make_bounded_role))
        else:
            checks.append(Check("Role with permissions boundary", "skipped (needs a managed policy)"))
        checks.append(_probe("Create IAM role", make_role))
    finally:
        for key in ("role", "role2"):
            if key in created:
                _quiet(lambda k=key: iam.delete_role(RoleName=created[k]))
        if "policy" in created:
            _quiet(lambda: iam.delete_policy(PolicyArn=created["policy"]))

    checks.append(_probe("GitHub OIDC login (IAM OIDC providers)", iam.list_open_id_connect_providers))
    account = sess.client("sts").get_caller_identity()["Account"]
    for label, client, call, kwargs in [
        ("ECS (Fargate)", "ecs", "list_clusters", {}),
        ("ECR (image registry)", "ecr", "describe_repositories", {}),
        ("DynamoDB (ledger)", "dynamodb", "list_tables", {}),
        ("SSM Parameter Store (secrets)", "ssm", "describe_parameters", {"MaxResults": 1}),
        ("CloudWatch Logs", "logs", "describe_log_groups", {"limit": 1}),
        ("EventBridge Scheduler", "scheduler", "list_schedules", {}),
        ("EventBridge rules (failure alerts)", "events", "list_rules", {"Limit": 1}),
        ("SNS (alert email)", "sns", "list_topics", {}),
        ("AWS Budgets", "budgets", "describe_budgets", {"AccountId": account, "MaxResults": 1}),
    ]:
        checks.append(_probe(label, lambda c=client, m=call, k=kwargs: getattr(sess.client(c), m)(**k)))

    by = {c.label: c for c in checks}
    advice = []
    if not by["Create own VPC (dry run)"].ok:
        advice.append("use_default_vpc = true")
    if (
        not by["Create managed IAM policy"].ok
        or not by.get("Role with permissions boundary", Check("", "")).ok
    ):
        advice.append("use_permissions_boundary = false")
    if not by["AWS Budgets"].ok:
        advice.append("create_budget = false")
    return checks, advice


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001, S110
        pass


def print_preflight(checks: list[Check], advice: list[str]) -> int:
    for c in checks:
        color = "green" if c.ok else "red" if c.status == "DENIED" else "yellow"
        say(f"{c.label:<42} {c.status}", color)
    say()
    say("GitHub OIDC isn't needed: leave github_oidc_provider_arn empty.")
    say("Push images with: usptrader-ops push-image")
    if advice:
        say("Add to infra/envs/prod/terraform.tfvars:", "cyan")
        for line in sorted(set(advice)):
            say(f"  {line}")
    else:
        say("No extra switches needed.", "green")
    vital = ("ECS", "ECR", "DynamoDB", "SSM", "Logs", "Scheduler", "Create IAM role")
    blocked = [c.label for c in checks if c.status == "DENIED" and any(v in c.label for v in vital)]
    if blocked:
        say("Blocked services the trader can't run without: " + ", ".join(blocked), "red")
        return 1
    return 0


# ---- secrets ----------------------------------------------------------------------------------
def read_clipboard() -> str:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001  fall back to the OS tools
        for cmd in (["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], ["pbpaste"],
                    ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):  # fmt: skip
            if shutil.which(cmd[0]):
                return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    raise OpsError(
        "Couldn't read the clipboard here. Run without --clipboard and paste at the hidden prompt."
    )


def clear_clipboard() -> None:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(" ")
        root.update()
        root.destroy()
    except Exception:  # noqa: BLE001, S110
        pass


def secret_names(ssm) -> list[str]:
    names: list[str] = []
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=f"/{NAME}", Recursive=True):
        names += [p["Name"] for p in page["Parameters"]]
    return sorted(names)


PLACEHOLDER = "CHANGE_ME"  # what Terraform stores until a real value is set


def secret_lengths(ssm) -> list[tuple[str, int | None]]:
    """(name, length) per secret; length is None while it still holds the Terraform placeholder."""
    out: list[tuple[str, int | None]] = []
    pages = ssm.get_paginator("get_parameters_by_path").paginate(
        Path=f"/{NAME}", Recursive=True, WithDecryption=True
    )
    for page in pages:
        out += [
            (p["Name"], None if p["Value"] == PLACEHOLDER else len(p["Value"])) for p in page["Parameters"]
        ]
    return sorted(out)


def fill_secrets(ssm, names: list[str], ask) -> list[tuple[str, int]]:
    """`ask(name)` returns the value or '' to skip. Returns (name, length) for what was set."""
    done = []
    for name in names:
        value = (ask(name) or "").strip()
        if not value:
            continue
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
        done.append((name, len(value)))
        say(f"  set ({len(value)} characters)", "green")
    return done


def cmd_secrets(sess, filter_: str, clipboard: bool) -> int:
    ssm = sess.client("ssm")
    names = [n for n in secret_names(ssm) if filter_ in n]
    if not names:
        raise OpsError(
            f"No secrets under /{NAME} matching {filter_!r}. Has terraform apply run in this region?"
        )

    if clipboard:

        def ask(name):
            answer = (
                input(f"Copy the value for {name}, then press Enter (s + Enter to skip): ").strip().lower()
            )
            return "" if answer == "s" else read_clipboard()
    else:

        def ask(name):
            return getpass.getpass(f"{name} (Enter to skip): ")

    fill_secrets(ssm, names, ask)
    if clipboard:
        clear_clipboard()
    say()
    say("Stored lengths (values are never shown):")
    for name, length in secret_lengths(ssm):
        say(f"  {name:<52} {'NOT SET' if length is None else length:>7}", "yellow" if length is None else "")
    return 0


# ---- push-image -------------------------------------------------------------------------------
PLATFORMS = {"X86_64": "linux/amd64", "ARM64": "linux/arm64"}


def task_platform(ecs) -> str:
    td = ecs.describe_task_definition(taskDefinition=NAME)["taskDefinition"]
    arch = (td.get("runtimePlatform") or {}).get("cpuArchitecture", "X86_64")
    return PLATFORMS.get(arch, "linux/amd64")


def write_docker_config(target: Path, registry: str, auth_b64: str, real: Path | None = None) -> None:
    """A throwaway Docker config holding the ECR login: no credential store at all.

    Windows Credential Manager rejects ECR's long tokens ("The stub received bad data"), and with
    no logins listed docker falls back to it on its own, so the login is written straight into
    config.json. The user's settings, contexts and builders are carried over.
    """
    real = real or Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")
    target.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if (real / "config.json").exists():
        try:
            cfg = json.loads((real / "config.json").read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            cfg = {}
    for key in ("credsStore", "credHelpers", "auths"):
        cfg.pop(key, None)
    cfg["auths"] = {registry: {"auth": auth_b64}}
    (target / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    for sub in ("contexts", "buildx", "cli-plugins"):
        if (real / sub).is_dir():
            shutil.copytree(real / sub, target / sub, dirs_exist_ok=True)


@contextmanager
def temp_docker_config(registry: str, auth_b64: str):
    tmp = Path(tempfile.mkdtemp(prefix="usptrader-docker-"))
    try:
        write_docker_config(tmp, registry, auth_b64)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def repo_root() -> Path:
    here = Path.cwd()
    for p in [here, *here.parents]:
        if (p / "Dockerfile").exists() and (p / "pyproject.toml").exists():
            return p
    raise OpsError("Run this from inside the us-paper-trader folder (it needs the Dockerfile).")


def cmd_push_image(sess) -> int:
    if not shutil.which("docker"):
        raise OpsError("docker isn't on PATH. Start Docker Desktop (or install it) and try again.")
    root = repo_root()
    ecr, ecs = sess.client("ecr"), sess.client("ecs")
    try:
        repo_uri = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]["repositoryUri"]
    except Exception as exc:  # noqa: BLE001
        raise OpsError(
            f"No ECR repository named {NAME}: run terraform apply first ({error_code(exc)})."
        ) from exc
    platform = task_platform(ecs)
    data = ecr.get_authorization_token()["authorizationData"][0]
    registry = data["proxyEndpoint"].removeprefix("https://")
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True, check=False).stdout.strip() or "manual"  # fmt: skip

    say(f"Building {platform} image from {root} ...")
    with temp_docker_config(registry, data["authorizationToken"]) as cfg:
        env = {**os.environ, "DOCKER_CONFIG": str(cfg)}
        cmd = ["docker", "buildx", "build", "--platform", platform, "--provenance=false",
               "-t", f"{repo_uri}:live", "-t", f"{repo_uri}:{sha}", "--push", str(root)]  # fmt: skip
        result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        raise OpsError("Image build or push failed (is Docker Desktop running?). The output above says why.")
    say(f"Pushed {repo_uri}:live ({sha}). The next scheduled or manual run uses it.", "green")
    return 0


# ---- run --------------------------------------------------------------------------------------
def network_from_schedule(scheduler) -> dict:
    """The subnets and security group the scheduled jobs use, read from the premarket schedule."""
    try:
        sched = scheduler.get_schedule(Name=f"{NAME}-premarket", GroupName=NAME)
    except Exception as exc:  # noqa: BLE001
        raise OpsError(f"Couldn't read the {NAME}-premarket schedule: run terraform apply first.") from exc
    target = json.loads(sched["Target"]["Input"])
    aws = target["NetworkConfiguration"]["AwsvpcConfiguration"]
    return {
        "awsvpcConfiguration": {
            "subnets": aws["Subnets"],
            "securityGroups": aws["SecurityGroups"],
            "assignPublicIp": aws.get("AssignPublicIp", "ENABLED"),
        }
    }


def cmd_run(sess, command: list[str], wait_seconds: int = 900) -> int:
    ecs, logs = sess.client("ecs"), sess.client("logs")
    network = network_from_schedule(sess.client("scheduler"))
    resp = ecs.run_task(
        cluster=NAME,
        taskDefinition=NAME,
        launchType="FARGATE",
        count=1,
        networkConfiguration=network,
        overrides={"containerOverrides": [{"name": "trader", "command": command}]},
    )
    if resp.get("failures") or not resp.get("tasks"):
        raise OpsError(f"Fargate didn't start the task: {resp.get('failures')}")
    task_arn = resp["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    say(f"started {' '.join(command)} as task {task_id}, waiting for it to finish...")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=NAME, tasks=[task_arn], WaiterConfig={"Delay": 6, "MaxAttempts": max(1, wait_seconds // 6)}
    )
    task = ecs.describe_tasks(cluster=NAME, tasks=[task_arn])["tasks"][0]
    container = task["containers"][0]
    exit_code = container.get("exitCode")

    stream = f"trader/trader/{task_id}"
    for _ in range(5):  # logs can lag the task's stop by a few seconds
        try:
            events = logs.get_log_events(
                logGroupName=f"/ecs/{NAME}", logStreamName=stream, startFromHead=True
            )
            for e in events["events"]:
                say(e["message"])
            break
        except Exception as exc:  # noqa: BLE001
            if error_code(exc) != "ResourceNotFoundException":
                raise
            time.sleep(3)
    if exit_code is None:
        say(
            f"The task stopped without running: {task.get('stoppedReason') or container.get('reason')}", "red"
        )
        return 1
    say(f"exit code: {exit_code}", "green" if exit_code == 0 else "red")
    return int(exit_code)


# ---- entry point ------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="usptrader-ops", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)  # fmt: skip
    p.add_argument(
        "--profile", default=os.environ.get("AWS_PROFILE"), help="AWS profile (default: AWS_PROFILE)"
    )
    p.add_argument("--region", default=os.environ.get("AWS_REGION") or DEFAULT_REGION)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setup-profile", help="add a self-refreshing AWS profile named usptrader")
    sp.add_argument("--source", default="default", help="the profile you sign in to with `aws login`")
    sub.add_parser("preflight", help="check what the account allows before terraform apply")
    ss = sub.add_parser("secrets", help="fill the SSM secrets")
    ss.add_argument("filter", nargs="?", default="", help="only names containing this text")
    ss.add_argument("--clipboard", action="store_true", help="read each value from the clipboard")
    sub.add_parser("push-image", help="build and push the image as :live")
    sr = sub.add_parser("run", help="run one trader command on Fargate now")
    sr.add_argument("command", nargs=argparse.REMAINDER, help="e.g. premarket | trade | postclose | report")
    args = p.parse_args(argv)

    try:
        if args.cmd == "setup-profile":
            path = setup_profile(args.source, args.region)
            say(f"Profile '{PROFILE}' is in {path}.", "green")
            say("Use it in this terminal with:  $env:AWS_PROFILE = 'usptrader'")
            say("  (bash/zsh: export AWS_PROFILE=usptrader)")
            say("Terraform and usptrader-ops then fetch fresh keys by themselves;")
            say("you only run 'aws login' when they say the sign-in itself has expired.")
            return 0
        if args.cmd == "run" and not args.command:
            raise OpsError("say what to run, e.g.: usptrader-ops run premarket")
        sess = session(args.profile, args.region)
        account = check_signed_in(sess)
        say(f"AWS account {account}, region {args.region}")
        if args.cmd == "preflight":
            return print_preflight(*preflight(sess))
        if args.cmd == "secrets":
            return cmd_secrets(sess, args.filter, args.clipboard)
        if args.cmd == "push-image":
            return cmd_push_image(sess)
        if args.cmd == "run":
            if not args.command:
                raise OpsError("say what to run, e.g.: usptrader-ops run premarket")
            return cmd_run(sess, args.command)
    except OpsError as exc:
        say(str(exc), "red")
        return 2
    except KeyboardInterrupt:
        say("cancelled")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
