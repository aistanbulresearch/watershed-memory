"""Render the bounded Runtime proof resources; importing this module makes no AWS calls."""

import re
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeSpec:
    account: str
    region: str
    name: str
    model_id: str
    artifact_sha256: str

    def __post_init__(self):
        patterns = {
            "account": r"[0-9]{12}", "region": r"[a-z]{2}-[a-z]+-[0-9]",
            "name": r"watershed_memory_[a-zA-Z0-9_]{1,31}",
            "model_id": r"amazon\.nova-pro-v1:0", "artifact_sha256": r"[0-9a-f]{64}",
        }
        for field, pattern in patterns.items():
            if not re.fullmatch(pattern, getattr(self, field)):
                raise ValueError(f"Invalid proof {field}.")


def build_plan(spec: RuntimeSpec) -> dict:
    """Pin code, one model and scoped role resources; the caller reviews before applying."""
    account, region, name = spec.account, spec.region, spec.name
    bucket = f"watershed-memory-proof-{account}-{region}"
    key = f"runtime/{spec.artifact_sha256}.zip"
    role = f"{name}_execution"
    role_arn = f"arn:aws:iam::{account}:role/{role}"
    runtime_pattern = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{name}-*"
    logs = f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/{name}-*"
    tags = {"Project": "watershed-memory", "Purpose": "bounded-feasibility"}
    statements = [
        {"Sid": "BedrockModel", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": [f"arn:aws:bedrock:{region}::foundation-model/{spec.model_id}"]},
        {"Sid": "ReadArtifact", "Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": [f"arn:aws:s3:::{bucket}/{key}"]},
        {"Sid": "RuntimeLogs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"], "Resource": [logs]},
        {"Sid": "RuntimeLogEvents", "Effect": "Allow",
         "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [f"{logs}:log-stream:*"]},
        {"Sid": "RuntimeTracePolicy", "Effect": "Allow",
         "Action": ["logs:PutResourcePolicy"], "Resource": [logs]},
        {"Sid": "DescribeLogs", "Effect": "Allow", "Action": ["logs:DescribeLogGroups"],
         "Resource": [f"arn:aws:logs:{region}:{account}:log-group:*"]},
        # These X-Ray operations do not support resource-level permissions.
        {"Sid": "Tracing", "Effect": "Allow", "Resource": ["*"],
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules", "xray:GetSamplingTargets"]},
        {"Sid": "RuntimeMetrics", "Effect": "Allow", "Resource": "*",
         "Action": ["cloudwatch:PutMetricData"],
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    ]
    return {
        "schema_version": 1, "bucket": bucket, "object_key": key, "role_name": role,
        "artifact_sha256": spec.artifact_sha256, "tags": tags,
        "trust_policy": {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole", "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": runtime_pattern},
            },
        }]},
        "execution_policy": {"Version": "2012-10-17", "Statement": statements},
        "create_runtime": {
            "agentRuntimeName": name,
            "description": "Watershed Memory: a bounded Strands review with an external case ledger.",
            "agentRuntimeArtifact": {"codeConfiguration": {
                "code": {"s3": {"bucket": bucket, "prefix": key}},
                "runtime": "PYTHON_3_12",
                "entryPoint": ["runtime/launch.py"],
            }},
            "roleArn": role_arn, "networkConfiguration": {"networkMode": "PUBLIC"},
            "protocolConfiguration": {"serverProtocol": "HTTP"},
            "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 300},
            "environmentVariables": {
                "WATERSHED_MODEL_ID": spec.model_id, "WATERSHED_AWS_REGION": region,
                "OTEL_SERVICE_NAME": name, "AGENT_OBSERVABILITY_ENABLED": "true",
                "OTEL_PYTHON_DISTRO": "aws_distro", "OTEL_PYTHON_CONFIGURATOR": "aws_configurator",
            },
            "clientToken": str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"watershed-memory:{account}:{region}:{name}:{spec.artifact_sha256}")),
            "tags": tags,
        },
        "log_retention_days": 7,
        # The AWS Create API omits this field; inspect it and explicitly update if needed.
        "runtime_metadata": {"requireMMDSV2": True},
    }


def transaction_search_policy(account: str, region: str) -> dict:
    """Account-scoped X-Ray delivery; only apply after inspecting existing configuration."""
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region):
        raise ValueError("Invalid tracing account or region.")
    return {"Version": "2012-10-17", "Statement": [{
        "Sid": "WatershedProofXRayDelivery", "Effect": "Allow",
        "Principal": {"Service": "xray.amazonaws.com"}, "Action": "logs:PutLogEvents",
        "Resource": [f"arn:aws:logs:{region}:{account}:log-group:{group}:*"
                     for group in ("aws/spans", "/aws/application-signals/data")],
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account}:*"}},
    }]}
