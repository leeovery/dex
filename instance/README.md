# <instance name>

A private **[dex](https://github.com/leeovery/dex)** instance: a knowledge base
maintained by Claude.

What this dex reads for is its lens: [`lens.md`](./lens.md). Ask Claude to
change it whenever you like.

**How to use it** (no technical steps, ever):

- **Save something** — share a link with your "Send to Dex" shortcut (or paste it
  into a Claude session and say "add this to dex"). It gets fetched, filed with
  who/when/why, and woven into the wiki.
- **Ask questions** — open a Claude Code session on this folder and ask.
  Answers come from the wiki with citations, newest first.
- **Health check** — it checks itself over on a schedule (broken links, stale
  pages, contradictions get found and fixed); you can also ask anytime.

The machinery lives in the shared engine and never needs touching.

## Run it on another machine

New laptop, or a second person joining? Open Claude Code in the
[Claude desktop app](https://claude.com/download), point the chat at where you
keep your code, and paste:

```
Fetch and follow the instructions at https://raw.githubusercontent.com/leeovery/dex/main/docs/start.md — this is an existing dex at <owner>/<repo>.
```

It clones this repo, checks the dependencies, arms the schedule, and sets up
phone capture. Everything else is already in the repo.
