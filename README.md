# Watershed Memory

## Three storms. One memory.

**A wildfire changes more than the landscape. It changes the work of protecting a drinking-water source.**

In July 2022, Las Vegas, New Mexico declared a disaster after flooding, ash and fire debris damaged infrastructure and threatened its water supply. Its water team had to keep monitoring an evolving watershed. [New Mexico Environment Department](https://www.env.nm.gov/wp-content/uploads/2022/08/2022-08-03-COMMS-City-of-Las-Vegas-drinking-water-remains-safe-to-drink-Final.pdf).

Every new observation arrives alongside earlier measurements, unfinished reviews and changing evidence coverage. The work has to stay connected from one storm to the next.

**Watershed Memory keeps that work connected.** One watershed case carries observations and operator responses forward. A later event strengthens an existing review; missing station evidence gets its own review. The next storm arrives. The work stays connected.

Built by **AIstanbul Research Group** for the **Agents for Humans Hackathon**, Professional Agents track.

### Try the operator workspace

With [uv](https://docs.astral.sh/uv/) and Python 3.12:

```bash
git clone https://github.com/aistanbulresearch/watershed-memory.git
cd watershed-memory
uv sync --frozen
uv run watershed-memory
```

Open **http://127.0.0.1:8765**. The small historical observation bundle is included; the browser experience needs no AWS credentials or large download.

1. **Start the replay.** July observations create a source-water review.
2. **Bring in August.** The same unfinished review gains a second evidence link.
3. **Record an acknowledgment.** The operator's note stays with the work.
4. **Bring in September.** New observations join the case; absent P1 archive evidence opens a separate coverage review.
5. **Complete one review and reload.** The completed work, remaining review and saved response are still there.

The workspace identifies **historical replay · rules**. Operator responses are demonstration actions. The records are real SQLite writes; each browser replay has an independent case.

### The agent behind the case

The Strands integration exposes three bounded tools: read saved case context, retrieve a released observation window, and propose a review against an explicitly selected unfinished task. The service validates the work and its evidence before committing the turn. A model cannot write an operator response or declare the watershed recovered.

The installed SDK is tested through its actual model/tool protocol, including context-dependent task selection and failure handling. A separate command runs the three-case gate against real Bedrock credentials and saves call, usage, tool and persistence evidence. [Run the Strands gate](docs/ENGINEERING.md#real-strands-gate).

**A fresh AgentCore session can receive the same enduring case.** The Runtime adapter, strict proposal contract and instrumented deployment package are implemented with local protocol tests. Cloud execution is a separate acceptance checkpoint. [Explore the Runtime boundary](docs/AGENTCORE.md).

### Engineering worth opening

- **One enduring case:** observations, reviews and operator responses stay connected across restarts.
- **Explicit agent decisions:** the agent selects the existing task from saved context; completed work is never reopened implicitly.
- **Atomic turns:** tools stage proposals; the complete validated turn commits together.
- **Execution claims:** concurrent duplicate requests share one planner execution, with a durable claim and replayable receipt.
- **Evidence checks:** unreleased observations are unavailable to tools; recorded results are checked against case and source packets.
- **An operator experience:** accessible actions, saved responses, contextual next steps, and evidence/trace panels on demand.

[Architecture](docs/ARCHITECTURE.md) · [Implementation and checks](docs/ENGINEERING.md) · [Source attribution](THIRD_PARTY_NOTICES.md)

### Check it

```bash
uv run pytest -q
node --test tests/request-state.test.mjs
uv run ruff check watershed_memory tests runtime deployment feasibility/run_strands.py
python -m unittest discover -s feasibility -p "test_*.py"
```

Product tests cover the HTTP journey, isolation, failure atomicity, adversarial planner output, request claims and the Strands SDK protocol. The original proof remains reproducible, with **21 tests and 11 checks across eight fresh processes**. [Original replay guide](feasibility/README.md).

### License

[MIT](LICENSE) · Copyright 2026 AIstanbul Research Group. The included extract retains its [source attribution and CC BY 4.0 notice](watershed_memory/data/NOTICE.md).
