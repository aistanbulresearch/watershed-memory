# A new Runtime session. The same watershed case.

The cloud planner receives the work that still matters, uses Strands tools to select the next review, and returns a verifiable proposal. The case service saves that work outside the Runtime session. The following storm can arrive in a different session and continue the same review.

## The division of work

```mermaid
sequenceDiagram
    participant Operator
    participant Case as Case service + SQLite
    participant Runtime as AgentCore Runtime
    participant Agent as Strands + Bedrock
    Operator->>Case: Bring in the next observation
    Case->>Case: Claim request; read saved case
    Case->>Runtime: New session; bounded context + evidence
    Runtime->>Agent: Retrieve context and propose review
    Agent-->>Runtime: Staged proposals + tool trace
    Runtime-->>Case: Bound response; no database writes
    Case->>Runtime: Request session stop
    Case->>Case: Validate; commit case + receipt
    Case-->>Operator: Updated review and supporting evidence
```

This transport has executed on AgentCore. In the verified September 12 run, August and September used separate Runtime sessions, retained one review, preserved the operator acknowledgment and opened a separate gap review. Duplicate requests did not invoke again, and a fresh process restored the same case. [Open the execution evidence](VERIFIED_RUN.md).

The current verified build uses Strands 1.55.1, Amazon Nova Pro, instruction `watershed-review-v3` and named endpoint `proof_v2` pinned to Runtime version 2. Memory lives in the external case ledger. AgentCore Runtime supplies the bounded execution environment; AgentCore-native Memory is not required for this workflow.

## Run a bounded live workspace

Authenticate AWS locally, then choose an explicit model, account and region. The browser receives case data; AWS credentials remain in the server process.

```bash
uv run watershed-memory --provider bedrock --profile YOUR_PROFILE --expected-account YOUR_ACCOUNT_ID --region us-east-1 --model-id amazon.nova-pro-v1:0 --database .local/runtime/live.sqlite --live-turn-limit 6
```

For a deployed Runtime, supply its ARN, a named endpoint and the version you reviewed:

```bash
uv run watershed-memory --provider agentcore --profile YOUR_PROFILE --expected-account YOUR_ACCOUNT_ID --region us-east-1 --model-id amazon.nova-pro-v1:0 --runtime-arn YOUR_RUNTIME_ARN --runtime-endpoint YOUR_NAMED_ENDPOINT --runtime-version YOUR_VERIFIED_RUNTIME_VERSION --database .local/runtime/agentcore.sqlite --live-turn-limit 6
```

The workspace binds to loopback by default. Explicit external binding also requires exact hosts, HTTPS origins and request limits; see the [interactive hosting guide](HOSTING.md). Its live-turn allowance is stored in the selected SQLite database and shared across browser sessions and server restarts. The counter is reserved atomically before inference; failed, interrupted and uncertain turns still consume an attempt. Saved request receipts return before the planner and consume no additional allowance. Each Strands turn also has its own call, output and time limits. Historical rules replay is the default when no provider is specified.

`--live-budget-scope` names an explicit allowance; its default is `local-live-demo-v1`. Reopening the same database and scope retains the count and requires the original `--live-turn-limit`. There is no automatic renewal. Choose a new scope only for a deliberately authorized new run. This is an application invocation control, not an AWS billing cap: a deployment must keep the scope server-controlled and the database protected. [Durable counter](../watershed_memory/persistent_budget.py) · [Concurrency and process-death tests](../tests/test_persistent_budget.py).

## Build and inspect the Runtime package

The deployment uses a Python 3.12 Linux ARM64 CodeZip. It includes the Strands planner, typed transport contract, attributed observation packets, AWS OpenTelemetry distribution and dependency notices. The UI, case database, credentials and director records are outside the archive.

Use a new, empty dependency target for each build; an existing target can retain packages from an older install. The example below assumes `temp/agentcore-build/dependencies` does not yet exist.

```bash
uv export --frozen --extra agentcore --no-dev --no-emit-project --no-hashes --output-file temp/agentcore-requirements.txt
uv pip install --python-version 3.12 --python-platform aarch64-manylinux2014 --only-binary :all: --target temp/agentcore-build/dependencies --requirements temp/agentcore-requirements.txt
uv run python -m deployment.build_runtime --dependencies temp/agentcore-build/dependencies --output temp/agentcore-build/runtime.zip
```

The builder checks native ELF architecture, rejects host binaries and private paths, and emits a SHA-256 manifest for every archive entry. It requires a fresh output path. `runtime/launch.py` starts the installed AWS OpenTelemetry instrumentation without depending on a host-generated console launcher.

`deployment.agentcore_spec` renders the dedicated IAM role, exact model and artifact permissions, content-addressed S3 object, Runtime configuration and five-minute maximum session lifetime. `deployment.provision` applies explicit phases with account, Free-plan, artifact and deadline checks. Each AWS operation is journaled before submission and after its response, so an uncertain outcome can be reconciled. No account upgrade is part of the deployment.

Verify that Runtime metadata requires MMDSv2 before invocation. If the explicit metadata phase updates the Runtime, AWS creates a new version. Use the returned and subsequently verified version when creating the named endpoint and starting the workspace.

For a source revision, `deployment.revise_runtime` verifies the previous configuration, allows the exact old and new artifact keys, updates the existing Runtime and creates a new version-pinned endpoint. It preserves earlier endpoints and artifacts so the original execution remains attributable to its original build.

## Follow the execution

The local receipt includes the tool trace, model and instruction versions, call count, aggregate token usage, verified Runtime endpoint version, AWS request identifiers when returned, and the session-stop request result. The Runtime emits structured correlation logs and is launched with AWS OpenTelemetry instrumentation. Request and evidence hashes have been correlated with scoped Runtime logs. The public trace exports only permitted tool fields and execution metadata; private log records and model text are excluded. Full account-wide span search additionally requires CloudWatch Transaction Search configuration.

[AWS CodeZip deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html) · [Runtime permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html) · [Observability setup](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
