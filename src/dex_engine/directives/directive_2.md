# Rewrite README.md from the engine's template

Until this engine release, an instance's README mirrored its scope list.
What the instance reads for now lives in `lens.md`, which directive 1
wrote from the owner's stated scope, and the engine's README template
links to that file in place of the list. An instance that stated no scope
has no `lens.md` and reads as a general knowledge dex, so its README says
that in place of the link. This directive replaces `README.md` once with
the template rendered for this instance: named for the instance's
directory, with the lens line that fits whether `lens.md` exists, and with
the repository in its "Run it on another machine" prompt taken from the
`origin` remote. An instance with no origin on GitHub has no repository a
second machine could clone, so its README carries no such section.

This directive edits `README.md` and nothing else.

The materials printed after these instructions, between the
`===== materials` line and the `===== end of materials =====` line, are
that README: the exact text `README.md` must hold.

1. **Read the old README.** Run `git log -1 --format=%H -- README.md` for
   the commit holding README.md as it stands, note the hash, and read the
   file whole. When README.md does not exist, there is no old README: go to
   step 3.
2. **List what it carried beyond the template.** Compare the old README
   with the materials:
   `bin/dex directive show 2 --materials | command diff README.md -`
   prints every line that differs, and exits 1 when any does. `command`
   runs the system's diff past any shell alias of that name, which would
   read these arguments differently. List every part of the
   old README the materials do not carry: its scope list and the sentences
   around it, its own opening line, anything the owner added, and anything
   else the materials leave out. A part the materials also carry, word for
   word or reworded, such as the list of ways to use the instance, needs
   no mention.
3. **Replace README.md** by running
   `bin/dex directive show 2 --materials > README.md`, which writes the
   materials byte for byte. Change nothing in the file afterwards: the
   check compares README.md with this same text, and any difference fails
   it, down to a single character or a missing final newline.
4. **Check and record** with `bin/dex directive done 2`. When it refuses,
   follow preparation step 5.
5. **Commit** `README.md` and `state/directives.jsonl` together as one
   commit. The subject is the first line `bin/dex directive show 2`
   printed, `directive 2: ` followed by this directive's intent. The body
   names what the old README carried beyond the template, so the owner can
   find it again:

   ```
   The old README is in history: git show <hash>:README.md

   Removed:
   - <part>: <why, in one line>
   ```

   Name each part by its heading, or by its first few words in quotes when
   it has none. The scope list's reason is that `lens.md` states what the
   instance reads for now; for anything else, say what it was. When there
   was no old README, or it already matched the materials, the whole body
   is one line saying so.

   Write the message to `cache/directive-2-message.txt` and commit with
   `git commit -F cache/directive-2-message.txt`, which keeps its quotes
   and backticks exactly as written.
