---
name: dex-capture
description: Capture something into this dex instance — a link, a file, an image, or a note handed over in any session. Use for "add this to dex", "save this", "capture this". Captures only; it never processes. Processing is dex-run's job.
---

# Capture

Capture and processing are separated — one route in, one route through.
This skill is the route in: it writes the same capture file the phone
shortcut writes, lands it on the remote, and stops. Processing is always
the dex-run skill, in full; there is deliberately no
process-just-this-item path. If the owner says "process now" or "process
the inbox", finish the capture, then run dex-run.

Invoked bare, with nothing in hand: ask "capture what?" and stop until
given a link, file, or note.

## Procedure

1. **Write the capture file**: `inbox/<yyyyMMdd-HHmmss>-<suffix>.md` — the
   current timestamp, then four random lowercase hex digits drawn with
   `od -An -N2 -tx1 /dev/urandom | tr -d ' \n'`, e.g.
   `inbox/20260818-101530-a3f9.md`. The suffix is not optional: this
   capture is committed from a working tree, and a same-second capture
   from anywhere else (the phone's names carry none) would meet it at the
   next pull as a conflict. Draw the digits with the command, never make
   them up — digits a session picks come out the same every time. A name
   already taken draws again; never overwrite a capture. Body = the
   captured URL and/or the owner's note, exactly as given — URL on the
   first line, note after, nothing else. The note is often the most
   valuable part; never trim or paraphrase it. Do not create a corpus
   item, fetch anything or judge the content: all of that is processing.

2. **A binary in hand** (an image, PDF, any file dropped into the
   session): file it the way `bin/dex inbox` files a staged one —
   - Path `media/<item-id>/<name>` where
     `item-id = sha1("inbox/<capture file>")[:6]`, the capture file being
     step 1's (`sha1("inbox/20260818-101530-a3f9.md")`). The key is the
     capture, never the file's name: names like `screenshot.png` recur,
     and a directory is one capture's alone.
   - If `media/<item-id>/` already exists, draw a new suffix for the
     capture file and key again.
   - `git add` the file and verify it staged as an LFS pointer
     (`git cat-file -p :media/<item-id>/<name>` starts with
     `version https://git-lfs`). If it did not, stop and report — a raw
     binary must not enter git history.
   - The capture file then carries the pointer the processor expects:

     ```
     ---
     media: media/<item-id>/<name>
     ---

     the owner's note
     ```

3. **Commit and push.** The capture is not finished until it is on the
   remote: captures are commits — scheduled runs and other machines pull
   the inbox, and an unpushed capture is invisible to all of them. Commit
   message: `capture: <short description>`. If the push fails, say so
   loudly and leave the commit in place — never quietly stop at a local
   commit. A local-only instance (no origin remote) has nowhere to push:
   the commit is the finished capture, and the skipped push is skipped,
   not failed.

4. **Confirm and stop.** One line: what was captured and that it is on the
   remote (or committed, on a local-only instance), e.g. `captured
   inbox/20260818-101530-a3f9.md — processed on the next run`. Do not process,
   enrich, digest, or touch the wiki. The next dex-run session (scheduled,
   or asked for) picks it up.
