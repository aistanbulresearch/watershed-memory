# Give each judge a continuing case

The [public walkthrough](https://aistanbulresearch.github.io/watershed-memory/) is available now without an account or model charges. It presents the verified case journey and the actual cloud receipts. The runnable operator desk adds a separate, interactive case for each browser session.

For an interactive deployment, keep the existing case service on **one application worker with a persistent SQLite volume**, behind an HTTPS ingress. The agent runs on the pinned AgentCore Runtime; the application owns the case, operator responses, execution claims and receipts.

## Prepare the application host

Use a persistent block volume with SQLite-compatible locking. Mount the database outside the application image and retain it through restarts and releases. Run one replica and one worker. The CLI explicitly selects one Uvicorn worker and disables its access log because session capabilities appear in request paths.

The HTTPS ingress must preserve the intended host, redact session paths from its own access logs, and enforce request-size and connection limits. Configure proxy trust for the chosen host; do not trust arbitrary forwarded headers. The application checks exact configured hosts and browser origins. Those checks complement the HTTPS ingress; the application process does not terminate TLS itself.

Give the server an AWS identity restricted to the approved Runtime and the endpoint checks required by the adapter. Credentials belong on the server. Do not place profiles, keys, account configuration or deployment manifests in browser assets.

## Select exposure explicitly

The ordinary command binds to loopback. Public binding requires an exact trusted host, exact HTTPS origin and explicit request limits. The following Linux-host example uses placeholders for the deployment selected by its operator:

```bash
uv run watershed-memory \
  --bind-host 0.0.0.0 \
  --trusted-host demo.example.com \
  --public-origin https://demo.example.com \
  --session-create-limit 10 \
  --post-limit 60 \
  --database /var/lib/watershed-memory/cases.sqlite \
  --provider agentcore \
  --region us-east-1 \
  --expected-account '<approved-account-id>' \
  --model-id amazon.nova-pro-v1:0 \
  --runtime-arn '<approved-runtime-arn>' \
  --runtime-endpoint '<approved-endpoint-name>' \
  --runtime-version '<approved-version>' \
  --live-turn-limit 12 \
  --live-budget-scope judge-preview-v1
```

Replace the host and AWS placeholders before use. Choose the invocation allowance against the approved demonstration budget. Configuration is checked before the CLI configures the live planner.

The request limits are **global rolling-minute burst limits** for this application process. A session creation consumes both the session and all-POST allowances. Invalid hosts or origins are rejected before admission; admitted malformed requests consume the POST allowance. A rejected request returns `429` with `Retry-After` before it can enter the case service. Health reads do not invoke a model or consume POST admission.

Burst counters reset when the process restarts. The global POST cap also applies when the configured session cap is larger. Hosts use standard HTTPS origins without an explicit port; loopback requests must match their exact local scheme, host and port.

The separate **live invocation allowance is durable**. Its atomic reservation survives failures and process restarts in the same database and named scope. Restarting the application does not replenish it. A completed request receipt returns without another invocation. Fix the allowance scope in the server configuration and protect the persistent volume; replacing the database or selecting a new scope creates a different allowance. This counter bounds attempts, not an AWS invoice.

## Keep useful proof in the browser

API responses retain the operator's own case, notes and source/tool evidence. A dedicated projection keeps the defined top-level browser fields and an approved execution receipt. The receipt shows the model, SDK, call count, usage, verified Runtime version and session-stop status, without publishing Runtime ARNs, private request IDs or endpoint identifiers. Canonical receipts remain intact in the server ledger.

Browser session IDs are bearer capabilities. Treat the session URL and local browser storage as private to that demonstration user. Configure anonymous-session and note retention before opening a shared host. Upstream access logs and backups require the same care as the database.

## Accept the actual deployment

Before giving judges the interactive address:

- Run a complete three-observation journey in two independent browser sessions; responses and unfinished work must remain isolated.
- Restart the application and verify the same saved case and consumed invocation count. Rehearse a consistent SQLite backup and restore.
- Verify the exact public hostname, HTTPS certificate, origin behavior and proxy trust on the deployed ingress.
- Confirm duplicate requests return a saved receipt and exhausted capacity starts no new model turn.
- Inspect browser responses and application/ingress logs for private infrastructure identifiers and session capabilities.
- Add ingress admission controls for fair access; one visitor should not monopolize the global application allowance. Keep the free recorded walkthrough available alongside the interactive experience.

The selected host, its storage and its ingress still require this deployment acceptance. The public recorded walkthrough and local operator desk are the currently verified access surfaces. [Architecture](ARCHITECTURE.md) · [AgentCore execution](AGENTCORE.md)
