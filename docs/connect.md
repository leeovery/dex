# Connect dex to chat

This connects the owner's dex instances to the **Claude desktop app's chat**,
so they can ask their own knowledge base — and save to it — from any
conversation, without opening a Claude Code session on the folder. You do ALL
the work; the owner never runs commands. Report every blocker loudly and never
work around a failure silently.

It is one command and a relaunch. Everything before the command is working out
what to point it at.

---

## Step 1: Check the ground

- **The desktop app is installed and has been launched at least once.** The
  app writes its own config file; this never creates one. If it has never
  run, ask the owner to open it once, then carry on.
- **The instances are cloned on this machine.** This connects a local app to
  local folders — an instance that exists only on GitHub cannot be served
  from here. If the owner wants one that isn't on this machine yet, set it up
  first (`docs/start.md`, the "existing" path) and come back.
- **macOS.** Only the mac location of the app's config file is verified. On
  anything else, ask the owner where their `claude_desktop_config.json` lives
  and pass it to the command below as `--config <path>`; if nobody knows,
  stop and say so rather than guessing at a path.

---

## Step 2: Find the instances

An instance root holds `bin/dex`, `corpus/` and `state/`, and carries a
`.dex-engine-pin` naming the engine release it runs. The usual home is
`~/Code/dex`, one directory per instance, so look there first and then wider:

```bash
ls -d ~/Code/dex/*/ 2>/dev/null
find ~ -maxdepth 4 -name .dex-engine-pin -not -path '*/.*' 2>/dev/null
```

Then **confirm the list with the owner** before writing anything. Show the
full paths and ask whether that is all of them — a dex on an external drive,
a partner's instance shared on this machine, or one they deliberately want
left out of chat. The list you agree on is exactly what gets served, and
re-running this command later with a different list is how it changes.

---

## Step 3: Connect

Pick the **anchor**: the instance whose `bin/dex` the app will run. Any of
them works. Prefer the one that runs most often — its engine pin is the one
the server follows, and its scheduled run is what keeps that pin current.

From the anchor's directory, listing every instance in the agreed order:

```bash
cd {anchor}
bin/dex connect --instance {anchor} --instance {other} --instance {another}
```

The anchor defaults to the first `--instance`, which is why the command runs
from that folder. To anchor somewhere else, add `--anchor {path}`.

It prints what it wrote and where. If it refuses — an unreadable config, a
path that is not an instance — read the message out and fix the cause; it
writes nothing on any refusal, so there is no half-connected state.

---

## Step 4: Relaunch and try it

Ask the owner to **quit the app entirely** (⌘Q — closing the window is not
enough) and open it again. The app starts the server at launch, so nothing
appears until it restarts.

Then have them ask a plain question in a normal chat — something only their
dex would know, phrased the way they'd say it out loud. If the answer cites
their own material, it is working. If the app never reaches for dex, check
Step 5's first note before assuming anything is broken.

---

## Step 5: Hand-Off

Tell the owner, in a few lines:

- **Ask from any chat.** Questions that fall inside a dex's scope get
  answered from their own saved material, with the item ids cited. It reads
  the clones on this machine — nothing is uploaded and no key is stored.
- **"Save this to my dex"** mid-conversation drops a capture into that
  instance's inbox, exactly like the phone shortcut. The next run reads it
  and files it; the save itself is instant.
- **The `dex-query` prompt**, in the app's prompt picker, for a deep pull —
  it runs the full query procedure instead of the two or three probes a chat
  reaches for on its own.
- **Adding or removing an instance later** means asking for this again; it is
  the same one command with a different list.

---

## If something is wrong

- **`connect` is not a command.** The anchor is pinned to an engine release
  older than this feature. Run `bin/dex sync` in it (commit and push what it
  changes) and try again.
- **The server never appears.** The app only spawns servers at launch: quit
  fully and reopen. If it still doesn't, check the config file — the `dex`
  entry's `command` must be a `bin/dex` that exists.
- **It appears but fails to start.** The entry carries the PATH captured from
  the shell this ran in, which is what lets the app find `uvx` at all. If
  Homebrew moved, or `uv` was installed after connecting, re-run the Step 3
  command from a shell where `uvx` works.
- **A fix isn't showing up.** The server runs the engine version the anchor's
  pin names, and it is read at app launch — so after a sync bumps the pin,
  relaunch the app.
