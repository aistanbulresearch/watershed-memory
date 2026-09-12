# Watershed Memory

## Three storms. One memory.

**A wildfire changes more than the landscape. It changes the work of protecting a drinking-water source.**

Every new storm brings another set of observations to connect with earlier measurements, unfinished reviews and missing evidence. The source-water team needs the whole story to decide what to inspect next.

**Watershed Memory keeps that work connected.** It carries a watershed case from one storm to the next, links new observations to the review already open, and brings missing evidence into the operator's work queue.

Built by **AIstanbul Research Group** for the **Agents for Humans Hackathon**, Professional Agents track.

### Watch the story unfold

| A storm arrives | The case remembers | The operator moves the work forward |
|---|---|---|
| July observations open a review | August observations join the same unfinished task | The operator acknowledges the review |
| September adds new evidence | Missing upstream measurements create a separate evidence review | Completing one review leaves the remaining work visible |

The first working slice replays **three real Gallinas observation windows** through a persistent task engine. It makes actual database writes and verifies the result across **eight separate processes**. Human responses in this replay are demonstration actions.

### Run it

Python 3.10+; the current workflow uses the standard library.

```bash
git clone https://github.com/aistanbulresearch/watershed-memory.git
cd watershed-memory
python -m feasibility.fetch_data
python -m feasibility.reconcile
python -m feasibility.run_proof
```

The first run downloads approximately 138 MB of public source data. Open the generated `proof.html` in the run directory printed by the final command. It shows the executed sequence, saved tasks and verification results. [Replay guide](feasibility/README.md).

### The engineering behind the memory

- **Evidence with a trail:** source checksums, station identifiers, time windows and original row references travel with the observations.
- **Work that persists:** one case, linked events, unfinished tasks and operator responses survive process restarts.
- **Safe retries:** repeated events and repeated responses do not duplicate work; conflicting payloads are rejected.
- **Time-aware state:** delayed older observations cannot roll the current case backward.
- **Atomic changes:** task and evidence updates commit together, with rollback checks for failures.

The current build runs the deterministic data and task layers. **Next: one Strands agent that retrieves the case history, selects evidence tools and updates the permitted review workflow, followed by the operator web experience.**

[Architecture](docs/ARCHITECTURE.md) · [Explore the implementation](docs/ENGINEERING.md) · [Data sources](THIRD_PARTY_NOTICES.md)

### Check the core

```bash
python -m unittest discover -s feasibility -p "test_*.py" -v
```

The current suite contains **21 tests**. The replay adds **11 end-to-end checks**, including persistence, duplicate handling and the separation of completed reviews from outstanding work.

### License

[MIT](LICENSE) · Copyright 2026 AIstanbul Research Group. Public datasets retain their own licenses and attribution, described in [Third-party notices](THIRD_PARTY_NOTICES.md).
