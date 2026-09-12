"""Run the loopback operator workspace in an explicit replay or bounded live mode."""

import argparse
import re
from pathlib import Path

import uvicorn

from .api import create_app
from .budget import BoundedPlanner
from .planning import ReplayPlanner
from .service import Service


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--port", type=int, default=8765)
    command.add_argument("--database", type=Path, default=Path(".local/runtime/cases.sqlite"))
    command.add_argument("--provider", choices=("replay", "bedrock", "agentcore"), default="replay")
    command.add_argument("--profile")
    command.add_argument("--region")
    command.add_argument("--expected-account")
    command.add_argument("--model-id")
    command.add_argument("--runtime-arn")
    command.add_argument("--runtime-endpoint")
    command.add_argument("--runtime-version")
    command.add_argument("--live-turn-limit", type=int, default=6)
    return command


def aws_account(profile: str | None, region: str) -> str:
    import boto3
    from botocore.config import Config
    client = boto3.Session(profile_name=profile, region_name=region).client("sts", config=Config(
        connect_timeout=5, read_timeout=10, retries={"max_attempts": 0}))
    return client.get_caller_identity()["Account"]


def configured_planner(args, account_lookup=aws_account):
    if args.provider == "replay":
        return ReplayPlanner()
    if (not re.fullmatch(r"[0-9]{12}", args.expected_account or "")
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", args.region or "")
            or not args.model_id):
        raise ValueError("Live mode requires an explicit model, region and expected AWS account.")
    if not 1 <= args.live_turn_limit <= 12:
        raise ValueError("Choose a live-turn allowance from 1 to 12.")
    if args.provider == "agentcore" and not all((args.runtime_arn, args.runtime_endpoint, args.runtime_version)):
        raise ValueError("AgentCore requires a Runtime ARN, named endpoint and expected version.")
    if account_lookup(args.profile, args.region) != args.expected_account:
        raise ValueError("AWS account does not match the explicitly selected account.")
    if args.provider == "bedrock":
        from .strands_agent import bedrock_planner
        selected = bedrock_planner(args.model_id, args.region, args.profile)
    else:
        from .agentcore_client import AgentCoreClient
        selected = AgentCoreClient(args.runtime_arn, args.runtime_endpoint, args.region,
            expected_account=args.expected_account, expected_version=args.runtime_version,
            model_id=args.model_id, profile=args.profile)
    return BoundedPlanner(selected, args.live_turn_limit)


def main() -> None:
    command = parser()
    args = command.parse_args()
    try:
        planner = configured_planner(args)
    except ValueError as error:
        command.error(str(error))
    uvicorn.run(create_app(Service(args.database, planner)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
