<!-- README_SYNC: source=working-tree; updated=2026-10-25 -->

<p align="center">
  <a href="./README.md">简体中文</a> · English
</p>

# Companion

**An open-source engineering project exploring long-term companionship with AI.**

Companion tries to let an AI's replies draw on shared history through
**traceable long-term memory, relationship state, and a continuous-interaction model** —
and studies how those mechanisms affect what it says and does next.

> **Origin and design assumptions**
>
> Companion started from a question about relational AI: give an AI an initial persona
> and a limited past, let it live alongside one person for a long time — can it grow
> memories, judgements and behaviour that are its own?
>
> **That question is still the starting point, but the project is no longer just that
> experiment.** It is now an engineering project built around long-term memory,
> relationship state and continuous interaction — memory, relationship, persona,
> user model, self model and emotion are each becoming **runnable, testable and
> traceable** modules. What was revised by engineering practice is recorded in
> [`docs/feasibility.md`](docs/feasibility.md) (Chinese).

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![CI](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml/badge.svg)](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2ea44f.svg)](https://SC2005725.github.io/companion-memory-drift/)
[![Stars](https://img.shields.io/github/stars/SC2005725/companion-memory-drift?label=Stars&color=f5b301)](https://github.com/SC2005725/companion-memory-drift/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/SC2005725/companion-memory-drift)](https://github.com/SC2005725/companion-memory-drift/commits/main)

<p align="center">
  <a href="https://github.com/SC2005725/companion-memory-drift/stargazers">
    <img src="https://img.shields.io/badge/%E2%AD%90_Star-Bookmark%20it%20and%20help%20others%20find%20it-f5b301?style=for-the-badge" alt="Star this repo">
  </a>
</p>

**If this project is useful to you, a Star helps you find it again and helps others running similar experiments find it.**

**📖 Docs site: https://SC2005725.github.io/companion-memory-drift/**
**Detailed progress: [`docs/project-report.md`](docs/project-report.md)** ｜ **Live numbers: [`STATUS.md`](STATUS.md)**

---

## Thirty seconds: how it differs from a "wrapper chatbot"

| The usual approach | What Companion does |
|---|---|
| Store memories in a vector DB, recall by similarity | **Plain keyword + 2-gram scoring.** No vector DB, no embeddings, no extra dependencies |
| Let the model decide what to remember | **Rules decide everything** — classification, relationship derivation, user modelling and behaviour auditing are all deterministic code |
| Hard-code the persona into a prompt | The persona has three layers: **stable base (sha256-guarded) · developable layer · user model** |
| Make the AI "feel" emotions | **No pretending.** Emotional state is numeric bookkeeping, and the code says so explicitly — it is not a claim of subjective feeling |
| Edit chat history freely | **Raw data is immutable**: memories are append-only, carry sha256, and the program refuses to overwrite |

> ## 🚧 Closed beta
>
> **This project is in closed beta. It is for research and personal use only — not for production.**
>
> - **Interfaces and data formats may change at any time.** No backward-compatibility promise.
> - **Not yet validated by long-running real use** — known limitations are listed in full below.
> - **Do not use it with sensitive data**, and do not rely on it to keep anything important.
> - Issues are welcome, but **no fix timeline is promised**.
>
> If all you want is to see what happens when an AI is given a limited past, it runs today.

---

## ⚠️ Read this first

**This repository contains no real conversations, memories or user-model data.**

| Directory | Status | Notes |
|---|---|---|
| `conversation/` `memory/` `user_model/` `self_model/` `relationship/` `logs/` | **gitignored** | Holds *your* data; created on first run |
| `.env` | **gitignored** | Your API key. Never commit it |

The plant in the examples ("that jasmine") comes from a **de-identified demo** and contains no identifying information.

**This is not a finished product.** The project deliberately does not build an emotion engine, an avatar or a push system. Some capabilities have only been verified on isolated copies, never over long real use. Known limitations are listed honestly below.

---

## Core features

- **Memory** — versions are append-only and never overwritten; every version carries SHA-256. Automatic consolidation runs in batches of 20 turns.
- **Classification and tiers** — 8 categories plus `core` / `experience` / `archive` tiers.
- **Decay without forgetting** — time decay has a **floor** (0.35), so old memories are never dropped for being old. If a timestamp is unknown, the item **does not decay at all**.
- **Conflict handling** — when facts conflict, history and provenance are kept; real deletion requires evidence.
- **Relationship** — state is **derived deterministically** from an event stream (pure functions, replayable), not a counter.
- **Two models** — `user_model` (how she sees you) and `self_model` (how she adapts her own approach), both revised by her from real feedback. `self_model` is scoped to the **"how" layer** only.
- **Persona coordination** — a static stable base + static initial profile + a **dynamic expression adjustment**. Relationship affects tone, but cannot rewrite character.
- **Proactivity** — heartbeat checks plus pending intentions; **choosing not to act is also a decision, and it is logged.**
- **Tool layer** — three permission tiers (`read` / `write` / `execute`), every call accounted for, bound to no particular agent platform.

---

## Quick start

```bash
git clone https://github.com/SC2005725/companion-memory-drift
cd companion-memory-drift
python -m venv .venv
.venv/Scripts/activate                    # Windows
# source .venv/bin/activate               # macOS / Linux
pip install -r requirements.txt
cp .env.example .env                      # fill in your own API key
python src/main.py ui                     # open the local chat UI
```

### Verify it works without spending anything

```bash
python src/selftest_memory.py
python src/selftest_companion.py
python src/selftest_persona_state.py
python src/selftest_merge.py
python src/selftest_tools.py
python src/dryrun.py
```

---

## Three design principles

### 1. Few settings, much left open

The initial profile provides a **starting point**, not an endpoint. The character model does not chase "more information is more real" — over-designing turns her into a fixed character authored by someone else. Unknowns, blanks, and interests that have not formed yet are deliberately preserved.

### 2. Determinism over "cleverness"

Classification, relationship derivation, user modelling and behaviour auditing **all use reproducible rules**, not model judgement. The reason is practical: the value of this system is that it can answer **"why was this handled this way?"** Swap in model judgement and that question becomes unanswerable.

### 3. History is not tamperable

Memory versions, user-model snapshots and relationship snapshots are all **append-only**. Revised items are **marked as superseded, never deleted** — "why did I change my mind" must stay answerable forever.

---

## Launch

```bat
python src\main.py ui             :: open the local web UI (opens the browser)
python src\main.py                :: command-line chat
```

### Web UI

The local web UI is served by the standard library only — **no frontend framework, no extra dependency**.

```
python src\main.py ui --port 8765
```

It gives you a chat pane, a sidebar with status / memory / settings tabs, and **image attachment**
(pick, paste or drag). Images are stored content-addressed under `uploads/` and sent as
multimodal input.

---

## 1. What this is

A **relational AI companion** prototype: small, runnable, and portable.

It does not study "how to build a chatbot that talks better". It asks:
**give an AI an initial persona and a limited past, let it live alongside one person for a long time —
can it grow memories, judgements, behaviour and a relationship that are its own?**

---

## 2. Where it is now (stage by stage)

| Phase | Content | Status |
|---|---|---|
| 1–3 | Chat + persona + raw-data persistence | ✅ Done, measured |
| 4 | Event solidification | ✅ Done, measured |
| 5 | Memory + **memory-drift experiment** | ✅ Done; experiment finished, baseline fact survival **18/18** |
| 6 | Relationship (relationship state) | ✅ Done, verified in real chat |
| 7 | User model (how she sees the user) | ✅ Done, verified in real chat |
| 8 | Agency / Heartbeat (proactivity) | ✅ Done, verified in real chat |
| 9 | **Tools** | ⚠️ **Partly done, not frozen** |
| 10+ | Model router and later | ⚠️ Router implemented; later stages not started |

### Known unverified risks

- The **emotion write path has never fired in real use** — `emotion/` contains no state file yet.
- The **persona development loop has never fired** — `profile.json` has no `provisional` entry.
- **Whether relationship state actually changes her wording is not measured** (no trace for it yet).
- The **model adapter layer is implemented but not enabled**.

> These are not "broken" — they are **not yet verified**.
> Not producing an emotion proposal when the evidence is weak is probably the correct behaviour.

### Problems this development surfaced (fixed, but worth remembering)

- The persona stable layer is guarded by a runtime hash check that **aborts the session** on mismatch — not a warning.
- `verify` detects **accidental or casual tampering**, not deliberate forgery. The raw event table has no hash chain.
- `orphan_files` does not affect the `ok` verdict of `verify`.

---

## 3. Dependencies

**Runtime: two.** `openai` and `python-dotenv`. Nothing else.

No vector database, no embeddings, no new framework, no scheduler, no background tasks.

```bash
pip install -r requirements.txt
```

**Docs site only** (used by CI, not a runtime dependency): see `requirements-docs.txt`.

---

## 4. Install (six steps on a new machine)

```bat
:: 1) Confirm the Python version (should be 3.11 or later)
python --version
:: 2) Enter the project directory
cd <project-dir>
:: 3) Install dependencies
python -m pip install -r requirements.txt
:: If the network is slow, use a mirror:
:: python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
:: 4) Create .env and fill in the key
copy .env.example .env
:: Open .env in Notepad and put your key after LLM_API_KEY=
:: 5) Verify the environment
python src\main.py doctor
:: 6) Start chatting
python src\main.py
```

---

## 5. How to fill in `.env`

```ini
LLM_API_KEY=your-key-here
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```

`.env` is plain text on your machine and is gitignored. **Never share it.**

---

## 6. Commands

```bat
python src\main.py                          :: start chatting (default command)
python src\main.py chat                     :: same as above
python src\main.py doctor                   :: environment self-check (free)
python src\main.py doctor --online          :: additionally send one minimal request to verify key / model / network (costs a little)
python src\main.py verify                   :: check whether raw data has been altered
python src\main.py cost                     :: call count / tokens / estimated spend
python src\main.py events                   :: list recorded raw events
python src\main.py memory                   :: list memory versions (--show prints the latest items)
python src\main.py consolidate              :: consolidate memory into a new version (**costs money**)
python src\main.py recall                   :: recall probe: ask memory a question and record the answer
python src\main.py changelog                :: differences between versions (a drift observation point)
python src\main.py relationship             :: relationship state and recent events
python src\main.py usermodel                :: "the user in her eyes" (including superseded items)
python src\main.py heartbeat                :: run one heartbeat check (free)
python src\main.py heartbeat --dry-run      :: compute only, write nothing — see what it would decide
python src\main.py tools                    :: tool layer: permissions, available tools, call ledger
python src\main.py seed-baseline            :: rebuild the baseline event and the annotation checklist
python src\main.py selftest --clean         :: offline end-to-end self-test (free)
python src\report.py                        :: generate experiments/comparison-report.md
```

### Checks you can run without a key

```bat
python src\selftest_tools.py          :: tool layer
python src\selftest_memory.py         :: memory + identity retrieval
python src\selftest_companion.py      :: relationship + user model + heartbeat + identity
python src\dryrun.py                  :: full dry run (executed on a copy)
python src\main.py selftest --clean   :: end-to-end pipeline
```

---

## 7. Directory structure

```
.
├── src/                         code
│   ├── main.py                  command-line entry point
│   ├── chat.py                  the conversation loop (assembles every phase)
│   ├── llm.py                   OpenAI-compatible call + usage extraction + empty-reply retry
│   ├── persona.py               persona loading, system-prompt assembly, runtime guard
│   ├── config.py                settings.json / .env loading
│   ├── storage.py               append-only persistence (turns / events / memory / relationship / user model / heartbeat / tools)
│   ├── events.py                raw-event trigger rules
│   ├── memory.py                memory items, retrieval (keyword-weighted), version rendering
│   ├── changelog.py             version diffs (drift observation)
│   ├── consolidate.py           turns raw events into a memory version via one model call
│   ├── retrieve.py              per-turn retrieval, persisted (even 0 hits)
│   ├── probe.py                 recall probe (with / without memory)
│   ├── relationship.py          Phase 6: relationship events and state derivation
│   ├── user_model.py            Phase 7: the user in her eyes + revision trail
│   ├── heartbeat.py             Phase 8: heartbeat and proactivity decisions
│   ├── tools/                   Phase 9: the tool layer
│   │   ├── base.py              Tool / ToolResult / path boundary
│   │   ├── registry.py          registry, call entry, protocol parsing, call ledger
│   │   ├── builtin.py           built-in tools (read_file / run_python / take_note)
│   │   └── prompt.py            renders the tool list for the model
│   ├── verify.py                hash chain and event-file verification
│   ├── pricing.py               pricing and the cost ledger
│   ├── report.py                generates the comparison report
│   ├── dryrun.py                full dry run (runs on a copy of the project)
│   ├── selftest*.py             offline self-tests per layer
│   └── paths.py                 path resolution (everything relative to the project root)
├── config/
│   ├── settings.json            model params / pricing / trigger words / per-layer rules / tool permissions
│   └── prompts/consolidate.md   consolidation prompt (external, so it can be versioned)
├── personality/personality.json the initial persona (the program may not modify it during an experiment)
├── memory/
│   ├── raw/events/              raw events, one file per event + .sha256
│   ├── versions/                memory version snapshots v001.json ... (write-only, never edited)
│   └── changelog/               recorded diffs between versions
├── conversation/                SQLite database (turns / events / relationship / user model / tool ledger)
├── relationship/                relationship snapshots (versioned, never overwritten)
├── user_model/                  user-model snapshots (versioned; superseded items are kept in the file)
├── recall/                      recall-probe records (not user conversation, stored separately)
├── experiments/                 experiment design, records, comparison reports
├── docs/                        feasibility analysis
├── notes/                       output of the Phase 9 "take a note" tool (created at runtime)
├── logs/                        run log and cost ledger
├── .env.example                 empty template (the archive ships no .env)
├── requirements.txt
└── README.md
```

---

## 8. Why raw data cannot be changed

- Memory versions are **append-only**. `memory/versions/v004.json` and everything before it is never overwritten.
- Revised facts are **marked with `superseded_by`**, not deleted.
- **There is no command that deletes or modifies data.** That is deliberate: `python src\main.py selftest --clean` exists for test data, but **once a real experiment has started, deleting raw data destroys the experiment.**
- `python src\main.py verify` lets you check later that nothing changed.

---

## 9. How to confirm the thing actually works (offline self-tests)

Everything below is fully offline, needs no API key, and costs nothing:

```bash
python src/selftest_memory.py
python src/selftest_companion.py
python src/selftest_persona_state.py
python src/selftest_merge.py
python src/selftest_tools.py
python src/dryrun.py
```

`python src\wap_audit.py` additionally audits **wiring** — functions that only tests call,
i.e. capabilities that were unit-tested but never connected to a real path.

---

## 10. Memory-drift experiment (Phase 5)

```
Phase 5 experiment end (frozen snapshot, never back-filled): 67 turns / 8 raw events / 3 memory versions
Current state: see STATUS.md
```

---

## 11. Phase 9 tool layer

### The core is not tied to any tool platform

Tools sit on top. The core runs without them.

### Three permission tiers (from config, never hard-coded)

| Tier | Meaning |
|---|---|
| `read` | read-only (safe) |
| `write` | may write files |
| `execute` | may run local commands (**highest risk**) |

Permissions come from `config/settings.json`. Enabling `execute` only changes that file — it does not change code.

> ⚠️ **Enabling `execute` lets the model run arbitrary Python on your machine.**
> This is **not a sandbox** — it is a capability switch. Open it only if you accept the consequences.
> Every call lands in the `tool_calls` ledger, so it can be reviewed afterwards.

### Call ledger

Every tool call is recorded: name, arguments, result, error kind, whether it was a repeat. One turn executes at most 5 tools, so the model cannot loop forever.

---

## 12. Moving to another machine

Copy the project directory, then re-create `.env` and re-install dependencies. Your data lives in gitignored directories, so it moves with the folder.

---

## 13. Cost

`python src\main.py cost` reports call count, tokens and estimated spend.

The ledger is `logs/ledger.jsonl` — one record per call, including cache-hit tokens and latency.

> **Do not infer this project's spend from your account balance.** If you share an API key with other projects, only `cost` tells you the truth.

---

## 14. Known limitations

**Mechanisms that are deliberate trade-offs, not defects:**

- **Event detection uses keyword rules** — determinism first; it would rather miss a record than spend an extra model call.
- **Memory retrieval is keyword-weighted, not embedding-based** — deliberate, to avoid an unattributable "similarity" variable. The cost: paraphrases may not be recalled.
- **The hash chain does not cascade** — altering only turn N trips turn N, but turn N+1 stays valid.
- **`verify` detects accidental tampering, not deliberate forgery** — after rewriting the JSON, the database hash and the sidecar together, verification passes. Acceptable for a single-user local tool, **but do not treat it as tamper-proof evidence.**
- **`orphan_files` does not affect the `ok` verdict** of `verify`.

**Data safety:**

- **There is no delete or modify command** — deliberate.
- Any test that might touch data runs on a **copy** first.
- **Never point `--clean`-style commands at a real project.**

---

## Contributing

Two ways in:

- **Question or feature request** → [open an issue](https://github.com/SC2005725/companion-memory-drift/issues)
- **Want to send code** → read [CONTRIBUTING.md](CONTRIBUTING.md) first; see [SECURITY.md](SECURITY.md) for security reports

Not sure which template to use? [Pick one here](https://github.com/SC2005725/companion-memory-drift/issues/new/choose).

<p align="center">
  <a href="https://github.com/SC2005725/companion-memory-drift/stargazers">⭐ Star</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/releases">Watch releases</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/issues">Open an issue</a>
</p>

## License

[Apache License 2.0](LICENSE) — see [NOTICE](NOTICE) for details.