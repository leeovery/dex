# pipeline-kit

Driver-based ingestion machinery. The ledger is the work queue, not a
receipt log: every unit of work is a line with a status, runs drain
whatever is not terminal, and anything discovered mid-flight re-enters
the top as a new entry with provenance.

## Install

```sh
uv tool install pipeline-kit
```

## Why blocked is not dead

A 403 challenge heals on retry. A 404 does not. Recording the difference
is the whole game.
