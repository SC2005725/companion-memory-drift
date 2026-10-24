<!-- README_SYNC: source=working-tree; updated=2026-10-25 -->

<p align="center">
  <a href="./README.md">简体中文</a> · English
</p>

# Companion · A Memory-Drift Experiment for Relational AI

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![CI](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml/badge.svg)](https://github.com/SC2005725/companion-memory-drift/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2ea44f.svg)](https://SC2005725.github.io/companion-memory-drift/)
[![Stars](https://img.shields.io/github/stars/SC2005725/companion-memory-drift?label=Stars&color=f5b301)](https://github.com/SC2005725/companion-memory-drift/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/SC2005725/companion-memory-drift)](https://github.com/SC2005725/companion-memory-drift/commits/main)

> A minimal, runnable prototype of a **relational AI companion**: small, runnable, and portable.
>
> It is not about building a chatbot that talks better. The question is:
> **give an AI an initial persona and a limited past, let it live alongside one person for a long time —
> can it grow memories, judgements, behaviour and a relationship that are its own?**

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

> **This is a prototype, not a product.** Five chains (persona outlet / development loop /
> heartbeat / emotion / model adapters) are wired up and pass their own tests, but
> **none of them has been validated by long-running real use** — see
> [Known limitations](#known-limitations).

---

## What's inside

- **Deterministic-first.** Anything that can be a rule is a rule. Model judgement is used only where
  rules genuinely cannot reach (e.g. consolidating dialogue into memory items).
- **Auditable evidence chain.** Raw events → memory versions (append-only, sha256) → changelog diffs.
  `python src/main.py verify` proves the original data has not been altered.
- **A persona that is protected but not frozen.** The stable layer is guarded by a fingerprint;
  a separate "developing" layer is where change is allowed to happen, with evidence.
- **Cost visibility.** Every model call goes through one accounting entry point, so spend never leaks.

## Quick start

```bash
git clone git@github.com:SC2005725/companion-memory-drift.git
cd companion-memory-drift

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # then fill in your API key
```

### Verify it works without spending anything

```bash
python src/main.py selftest      # offline test suites — no API key required
python src/main.py verify        # confirm raw data is unchanged
python src/main.py doctor        # environment and configuration check
```

### Start talking

```bash
python src/main.py chat
python src/main.py ui --port 8765
```

## Commands

`python src/main.py <command>` — available commands:

```
chat          doctor        verify        cost          events
memory        recall        changelog     relationship  usermodel
selfmodel     identity      heartbeat     ui            router
archive-anchor  tools       selftest
```

## Architecture

```
src/
  main.py            CLI entry point
  chat.py            the per-turn pipeline
  memory.py          memory items, retrieval, decay, provenance, privacy
  consolidate.py     dialogue → memory versions (rides on one model call)
  persona.py         persona, initial profile, expression adjustment
  relationship.py    relationship state, derived from real events only
  self_model.py      how she adapts her own behaviour (feedback-driven)
  user_model.py      how she understands the user
  emotion.py         emotional state: vocabulary, decay, evidence gate
  heartbeat.py       proactive-check skeleton (deterministic decision rules)
  snapshots.py       shared snapshot semantics
  model_adapter.py   unified model adapter layer (local / free / existing)
  storage.py         SQLite schema and persistence
```

**Design principles**

1. **The core is not bound to any tool platform.** The tool layer sits on top; the core runs without it.
2. **Three permission tiers**, read from configuration rather than hard-coded in source.
3. **Everything is accounted for.** Model calls, tool calls and memory retrievals are all logged.

## Boundaries — what this project deliberately does not do

- **No emotion engine that claims feelings.** The emotion module is numeric state with an
  evidence gate. It does not assert subjective experience.
- **No vector database, no embeddings, no reranker.** Retrieval is keyword + 2-gram, by design.
- **No background scheduler and no push notifications.** The heartbeat only checks when you speak.
- **No real-user data in this repository.** `conversation/`, `memory/`, `user_model/`,
  `self_model/`, `relationship/`, `logs/` and `.env` are gitignored — they hold *your* data
  and are created on first run.
- The example plant ("that jasmine") comes from a **de-identified demo** and contains no identifying information.

## Known limitations

- The five chains are wired and unit-tested, but **not validated over long real usage**.
- The `provisional` development layer has never received a real proposal.
- The emotion module's write path is **not yet connected** to the conversation flow, so emotional
  state does not change during real chat today.
- The heartbeat has never produced a real proactive message yet — the trigger conditions simply
  have not been met.
- Several capabilities have only been verified on isolated copies.

## Contributing

Two ways in:

- **Question or feature request** → [open an issue](https://github.com/SC2005725/companion-memory-drift/issues)
- **Want to send code** → read [CONTRIBUTING.md](CONTRIBUTING.md) first; see [SECURITY.md](SECURITY.md) for security reports

Not sure which template to use? [Pick one here](https://github.com/SC2005725/companion-memory-drift/issues/new/choose).

## License

[Apache License 2.0](LICENSE) — see [NOTICE](NOTICE) for details.

---

<p align="center">
  <a href="https://github.com/SC2005725/companion-memory-drift/stargazers">⭐ Star</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/releases">Watch releases</a> ·
  <a href="https://github.com/SC2005725/companion-memory-drift/issues">Open an issue</a>
</p>