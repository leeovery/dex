# Connect dex to chat

This connects the owner's dex instances to the **chat clients on this
machine**, so they can ask their own knowledge base — and save to it — without
opening a session on the instance folder. You do ALL the work; the owner never
runs commands. Report every blocker loudly and never work around a failure
silently.

There are two clients, and they are independent. The owner may want one, both,
or neither:

- **The Claude desktop app's chat** — ask a dex from any ordinary conversation.
- **Claude Code, at user level** — every Claude Code session on this machine,
  in any project, can search the owner's dexes and capture into them. Inside an
  instance folder it is redundant (the session has the real files); everywhere
  else it is the only way a session sees dex at all.

It is one command, however many clients they say yes to.

---

## Step 1: Check the ground

- **Which clients are actually here.** Ask about nothing else:

  ```bash
  ls "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
  command -v claude
  ```

  The desktop app writes its own config at first launch, so a missing file
  means it has never run — ask the owner to open it once if they want that
  client, then carry on. A missing `claude` means Claude Code is not installed
  here; do not mention that client.
- **The instances are cloned on this machine.** This connects local clients to
  local folders — an instance that exists only on GitHub cannot be served from
  here. If the owner wants one that isn't on this machine yet, set it up first
  (`docs/start.md`, the "existing" path) and come back.
- **macOS.** Only the mac location of the desktop app's config is verified. On
  anything else, ask the owner where their `claude_desktop_config.json` lives
  and pass it as `--config <path>`; if nobody knows, connect Claude Code alone
  rather than guessing at a path. Claude Code's own location needs no flag.

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
full paths and ask whether that is all of them — a dex on an external drive, a
partner's instance shared on this machine, or one they deliberately want left
out of chat. The list you agree on is exactly what gets served, and re-running
this command later with a different list is how it changes.

---

## Step 3: Ask, per client

One question per client that is present. Either can be declined, and a no
costs nothing — Step 5 tells them how to come back.

- Desktop app: "do you want to ask this dex questions, and save to it, from
  ordinary chat?"
- Claude Code: "do you want the dex server installed at user level, so every
  Claude Code session on this machine can search and capture into your dexes?"

---

## Step 4: Connect

Pick the **anchor**: the instance whose `bin/dex` the clients will run. Any of
them works, and both clients use the same one. Prefer the instance that runs
most often — its engine pin is the one the server follows, and its scheduled
run is what keeps that pin current.

From the anchor's directory, listing every instance in the agreed order and
naming only the clients the owner said yes to:

```bash
cd {anchor}
bin/dex connect --client {desktop|code} [--client {the other}] \
  --instance {anchor} --instance {other} --instance {another}
```

The anchor defaults to the first `--instance`, which is why the command runs
from that folder. To anchor somewhere else, add `--anchor {path}`. Omitting
`--client` writes every client found on this machine — pass the flags
explicitly so a declined client stays declined.

It prints one line per client: what it wrote, or why it skipped. Read those
lines. If it refuses outright — an unreadable config, a path that is not an
instance — read the message out and fix the cause; it writes nothing on a
refusal, so there is no half-connected state.

---

## Step 5: Make it live, and try it

- **Desktop app** — ask the owner to **quit it entirely** (⌘Q; closing the
  window is not enough) and open it again. It starts the server at launch, so
  nothing appears until it restarts.
- **Claude Code** — new sessions pick it up on their own. Any session already
  open connected its servers at start and needs restarting; `/mcp` in a fresh
  session lists `dex`.

Then have them ask a plain question — something only their dex would know,
phrased the way they'd say it out loud. If the answer cites their own
material, it is working.

---

## Step 6: Hand-Off

Tell the owner, in a few lines:

- **Ask from any chat**, in whichever clients they connected. Questions that
  fall inside a dex's scope get answered from their own saved material, with
  the item ids cited. It reads the clones on this machine — nothing is
  uploaded and no key is stored.
- **"Save this to my dex"** mid-conversation drops a capture into that
  instance's inbox, exactly like the phone shortcut. The next run reads it and
  files it; the save itself is instant.
- **The `dex-query` prompt**, in the desktop app's prompt picker, for a deep
  pull — it runs the full query procedure instead of the two or three probes a
  chat reaches for on its own.
- **Changing anything later** — adding an instance, adding a client they
  declined today, or repairing a broken entry — means asking for this same
  paste again. One path, whatever changed.

---

## If something is wrong

- **`connect` is not a command, or `--client` is not a flag.** The anchor is
  pinned to an engine release older than this feature. Run `bin/dex sync` in it
  (commit and push what it changes) and try again.
- **The desktop server never appears.** The app only spawns servers at launch:
  quit fully and reopen. If it still doesn't, check the config file — the `dex`
  entry's `command` must be a `bin/dex` that exists.
- **`/mcp` doesn't list dex in Claude Code.** The session was already open when
  it was connected; start a new one. If a fresh session still doesn't show it,
  `claude mcp get dex` says what is filed.
- **It appears but fails to start.** The desktop entry carries the PATH
  captured from the shell this ran in, which is what lets the app find `uvx` at
  all. If Homebrew moved, or `uv` was installed after connecting, re-run the
  Step 4 command from a shell where `uvx` works.
- **A fix isn't showing up.** The server runs the engine version the anchor's
  pin names, read at client start — so after a sync bumps the pin, relaunch the
  app and start a new Claude Code session.
- **The anchor instance moved or was deleted.** Both clients now point at a
  `bin/dex` that isn't there, and a server that cannot start says nothing about
  it. Re-run Step 4 against an instance that is still here.
