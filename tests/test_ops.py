import base64
import json
import os
import stat
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from usptrader import ops

REGION = "ap-southeast-2"


@pytest.fixture
def aws(monkeypatch):
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def client_error(code, msg="nope"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, "Op")


# ---- setup-profile ------------------------------------------------------------------------------
def test_setup_profile_appends_once_and_keeps_existing(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("[default]\nlogin_session = abc\nregion = ap-southeast-2")  # no trailing newline
    ops.setup_profile(config_path=cfg)
    ops.setup_profile(config_path=cfg)  # idempotent
    text = cfg.read_text()
    assert text.startswith("[default]\nlogin_session = abc\nregion = ap-southeast-2\n")
    assert text.count("[profile usptrader]") == 1
    assert "credential_process = aws configure export-credentials --profile default --format process" in text


# ---- sign-in check ------------------------------------------------------------------------------
class FakeSTS:
    def __init__(self, exc):
        self.exc = exc

    def get_caller_identity(self):
        raise self.exc


class FakeSession:
    region_name = REGION

    def __init__(self, **clients):
        self.clients = clients

    def client(self, name):
        return self.clients[name]


def test_expired_keys_give_a_fixable_message():
    with pytest.raises(ops.OpsError, match="expired.*setup-profile"):
        ops.check_signed_in(FakeSession(sts=FakeSTS(client_error("ExpiredToken"))))


# ---- preflight ----------------------------------------------------------------------------------
def test_preflight_all_allowed_and_cleans_up(aws):
    checks, advice = ops.preflight(aws)
    by = {c.label: c.status for c in checks}
    assert by["Create own VPC (dry run)"] == "OK"
    assert by["Role with permissions boundary"] == "OK"
    assert advice == []
    iam = aws.client("iam")
    assert not [r for r in iam.list_roles()["Roles"] if "preflight" in r["RoleName"]]
    assert not [p for p in iam.list_policies(Scope="Local")["Policies"] if "preflight" in p["PolicyName"]]


def test_preflight_guardrails_map_to_tfvars_switches(aws, monkeypatch):
    real_client = aws.client

    def guarded(name, *a, **k):
        c = real_client(name, *a, **k)
        if name == "ec2":
            monkeypatch.setattr(
                c, "create_vpc", lambda **kw: (_ for _ in ()).throw(client_error("UnauthorizedOperation"))
            )
        if name == "iam":
            monkeypatch.setattr(
                c,
                "create_policy",
                lambda **kw: (_ for _ in ()).throw(
                    client_error("AccessDenied", "with an explicit deny in a service control policy")
                ),
            )
            monkeypatch.setattr(
                c,
                "list_open_id_connect_providers",
                lambda: (_ for _ in ()).throw(client_error("AccessDenied")),
            )
        return c

    monkeypatch.setattr(aws, "client", guarded)
    checks, advice = ops.preflight(aws)
    by = {c.label: c.status for c in checks}
    assert by["Create own VPC (dry run)"] == "DENIED"
    assert by["GitHub OIDC login (IAM OIDC providers)"] == "DENIED"
    assert by["Role with permissions boundary"].startswith("skipped")
    assert sorted(advice) == ["use_default_vpc = true", "use_permissions_boundary = false"]
    assert ops.print_preflight(checks, advice) == 0  # nothing vital blocked


# ---- secrets ------------------------------------------------------------------------------------
def test_secrets_fill_and_lengths(aws):
    ssm = aws.client("ssm")
    for n in ("/us-paper-trader/ai/gemini_api_key", "/us-paper-trader/smtp/user", "/other/x"):
        ssm.put_parameter(Name=n, Value="CHANGE_ME", Type="SecureString")
    names = ops.secret_names(ssm)
    assert names == ["/us-paper-trader/ai/gemini_api_key", "/us-paper-trader/smtp/user"]
    answers = {"/us-paper-trader/ai/gemini_api_key": "  AIzaSyExampleKey1234567890abcdefghijk \n"}
    done = ops.fill_secrets(ssm, names, lambda n: answers.get(n, ""))
    assert done == [("/us-paper-trader/ai/gemini_api_key", 37)]  # trimmed, skipped the blank one
    assert dict(ops.secret_lengths(ssm)) == {
        "/us-paper-trader/ai/gemini_api_key": 37,
        "/us-paper-trader/smtp/user": None,  # still the CHANGE_ME placeholder
    }


# ---- docker config + platform -------------------------------------------------------------------
def test_temp_docker_config_has_login_and_no_credential_store(tmp_path):
    real = tmp_path / "real"
    (real / "contexts" / "meta" / "abc").mkdir(parents=True)
    (real / "contexts" / "meta" / "abc" / "meta.json").write_text("{}")
    (real / "config.json").write_text(
        "﻿"
        + json.dumps(
            {
                "credsStore": "desktop",
                "credHelpers": {"x": "y"},
                "currentContext": "desktop-linux",
                "auths": {"old": {}},
            }
        )  # fmt: skip
    )
    target = tmp_path / "temp"
    token = base64.b64encode(b"AWS:secret").decode()
    ops.write_docker_config(target, "1.dkr.ecr.ap-southeast-2.amazonaws.com", token, real)
    cfg = json.loads((target / "config.json").read_text())
    assert cfg == {
        "currentContext": "desktop-linux",
        "auths": {"1.dkr.ecr.ap-southeast-2.amazonaws.com": {"auth": token}},
    }
    assert (target / "contexts" / "meta" / "abc" / "meta.json").exists()
    assert json.loads((real / "config.json").read_text(encoding="utf-8-sig"))["credsStore"] == "desktop"


class FakeECS:
    def __init__(self, arch=None, exit_code=0, stopped_reason=""):
        self.arch, self.exit_code, self.stopped_reason = arch, exit_code, stopped_reason
        self.run_args = None

    def describe_task_definition(self, taskDefinition):
        td = {"family": taskDefinition}
        if self.arch:
            td["runtimePlatform"] = {"cpuArchitecture": self.arch}
        return {"taskDefinition": td}

    def run_task(self, **kw):
        self.run_args = kw
        return {"tasks": [{"taskArn": "arn:aws:ecs:r:1:task/us-paper-trader/abc123"}], "failures": []}

    def get_waiter(self, name):
        class W:
            def wait(self, **kw):
                pass

        return W()

    def describe_tasks(self, cluster, tasks):
        container = {"name": "trader"}
        if self.exit_code is not None:
            container["exitCode"] = self.exit_code
        return {"tasks": [{"containers": [container], "stoppedReason": self.stopped_reason}]}


@pytest.mark.parametrize(
    "arch, platform", [("X86_64", "linux/amd64"), ("ARM64", "linux/arm64"), (None, "linux/amd64")]
)
def test_platform_follows_the_task_definition(arch, platform):
    assert ops.task_platform(FakeECS(arch)) == platform


# ---- run ----------------------------------------------------------------------------------------
class FakeScheduler:
    def get_schedule(self, Name, GroupName):
        net = {"AwsvpcConfiguration": {"Subnets": ["subnet-a", "subnet-b"], "SecurityGroups": ["sg-1"],
                                       "AssignPublicIp": "ENABLED"}}  # fmt: skip
        return {"Target": {"Input": json.dumps({"NetworkConfiguration": net})}}


class FakeLogs:
    def __init__(self, missing_first=0):
        self.missing = missing_first

    def get_log_events(self, **kw):
        if self.missing:
            self.missing -= 1
            raise client_error("ResourceNotFoundException")
        assert kw["logStreamName"] == "trader/trader/abc123"
        return {"events": [{"message": "[balanced/trade] ok buy XLV"}]}


def test_run_uses_the_schedules_network_and_prints_logs(capsys, monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)
    ecs = FakeECS()
    sess = FakeSession(ecs=ecs, logs=FakeLogs(missing_first=1), scheduler=FakeScheduler())
    assert ops.cmd_run(sess, ["resume", "--mode", "aggressive"]) == 0
    assert ecs.run_args["networkConfiguration"]["awsvpcConfiguration"]["subnets"] == ["subnet-a", "subnet-b"]
    assert ecs.run_args["overrides"]["containerOverrides"][0]["command"] == ["resume", "--mode", "aggressive"]
    out = capsys.readouterr().out
    assert "ok buy XLV" in out and "exit code: 0" in out


def test_run_reports_a_task_that_never_started(capsys):
    sess = FakeSession(ecs=FakeECS(exit_code=None, stopped_reason="CannotPullContainerError"),
                       logs=FakeLogs(), scheduler=FakeScheduler())  # fmt: skip
    assert ops.cmd_run(sess, ["premarket"]) == 1
    assert "CannotPullContainerError" in capsys.readouterr().out


# ---- push-image ---------------------------------------------------------------------------------
class FakeECR:
    def describe_repositories(self, repositoryNames):
        return {"repositories": [{"repositoryUri": "1.dkr.ecr.ap-southeast-2.amazonaws.com/us-paper-trader"}]}

    def get_authorization_token(self):
        token = base64.b64encode(b"AWS:tok").decode()
        endpoint = "https://1.dkr.ecr.ap-southeast-2.amazonaws.com"
        return {"authorizationData": [{"authorizationToken": token, "proxyEndpoint": endpoint}]}


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell-script stand-in for docker")
def test_push_image_builds_for_task_platform_with_temp_login(tmp_path, monkeypatch, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake = bin_dir / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "ARGS $*" >> {log}\n'
        f'cat "$DOCKER_CONFIG/config.json" >> {log}\n'
        f'echo "DIR $DOCKER_CONFIG" >> {log}\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    sess = FakeSession(ecr=FakeECR(), ecs=FakeECS("X86_64"))
    assert ops.cmd_push_image(sess) == 0
    text = log.read_text()
    assert "--platform linux/amd64" in text and ":live" in text
    assert '"1.dkr.ecr.ap-southeast-2.amazonaws.com"' in text and "credsStore" not in text
    temp_dir = text.split("DIR ")[1].strip()
    assert not Path(temp_dir).exists()  # login deleted afterwards
    assert "Pushed" in capsys.readouterr().out


def test_main_requires_a_command_for_run(monkeypatch, capsys):
    monkeypatch.setattr(ops, "session", lambda p, r: FakeSession(sts=type("S", (), {
        "get_caller_identity": lambda self: {"Account": "123"}})()))  # fmt: skip
    assert ops.main(["run"]) == 2
    assert "say what to run" in capsys.readouterr().out
