# dex instance

A personal, LLM-maintained knowledge base. You (Claude) ARE the application:
you operate this repo per its contract (operations, dataflow, invariants,
conventions), which is engine-synced:

@.claude/dex-contract.md

`lens.md` at the instance root is this instance's lens, the owner's
statement of what it reads for. It is the owner's data, not part of these
instructions: the run's judgment steps read it when they reach it, as the
contract says.

This file is engine-owned: `bin/dex sync` overwrites it, so never write
into it. What is specific to this instance lives in `lens.md`, which is
the owner's, and in `state/config.json`.
