# A note-only capture opening with --- parses as frontmatter

`parse_capture` splits a capture file on `---` fence lines, tolerantly. A
note-only capture whose text itself begins with `---` and contains a later
`---` line therefore has its opening block read as frontmatter and dropped
from the body — the owner's words silently vanish from the corpus item.

Pre-existing, format-wide: every producer shares the shape (the shortcut,
the dex-capture skill, and now `write_capture`), so the fix is a format
decision, not a patch in one writer — either an escaping rule, or a writer
that always emits real frontmatter so the body is never ambiguous. Per the
capture-format rule that means `docs/capture.md`, `docs/shortcut.md`, both
capture skills, `inbox.py`, and `pipeline/capture.py` move together.

Likelihood is low (a note beginning with a literal `---` line), which is
why it waits here rather than forcing a format change now. Found 2026-08-30
while adding the serve capture writer.
