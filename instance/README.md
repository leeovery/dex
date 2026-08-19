# <instance>

<Owner>'s <domain> knowledge base — a private **[dex](https://github.com/leeovery/dex)**
instance, maintained by Claude.

**How to use it** (no technical steps, ever):

- **Save something** — share a link with your "Send to Dex" shortcut (or paste it
  into a Claude session and say "add this to dex"). It gets fetched, filed with
  who/when/why, and woven into the wiki.
- **Ask questions** — open a Claude session on this folder (Cowork or Claude
  Code) and ask. Answers come from the wiki with citations, newest first.
- **Health check** — it checks itself over on a schedule (broken links, stale
  pages, contradictions get found and fixed); you can also ask anytime.

## In scope

- <topic>
- <topic>

Anything not listed is out of scope — Claude will ask rather than guess about
borderline finds. Want more topics covered? Just say so: scope changes update
both this README and [`CLAUDE.md`](./CLAUDE.md).

## Run it on another machine

Paste this into a Claude Code session on the new machine (the desktop app's
Code tab or the CLI):

> Fetch https://raw.githubusercontent.com/leeovery/dex/main/docs/start.md
> and follow it. My instance repo is `<owner>/<repo>`.

The machinery lives in the shared engine and never needs touching.
