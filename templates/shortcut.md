# "Send to Dex" iOS Shortcut

Share-sheet capture: any URL (tweet, article, video) → one line in `state/inbox.md`
via the `inbox` GitHub Action. The scheduled ingest op does the rest.

## One-time setup

1. GitHub → Settings → Developer settings → Fine-grained tokens → new token:
   repository access = `<owner>/<volume>` only, permissions = Contents: Read and write.
2. Shortcuts app → new shortcut → three actions:

   1. **Receive** input from Share Sheet (types: URLs, Text).
   2. **Ask for Input** (Text), prompt "Why? (optional)", allow empty — this becomes
      the note.
   3. **Get Contents of URL**:
      - URL: `https://api.github.com/repos/<owner>/<volume>/issues`
      - Method: POST
      - Headers:
        - `Authorization`: `Bearer <YOUR-PAT>`
        - `Accept`: `application/vnd.github+json`
      - Request body (JSON), two flat text fields:
        - `title` = Shortcut Input
        - `body` = Provided Input

   The PAT needs Issues: Read and write (in addition to Contents). The API
   returns real JSON (201) so errors are readable and there is no empty-204
   parse quirk. The inbox Action appends the line and auto-closes the issue.
   (The older repository_dispatch route still works too.)

3. Name it "Send to Dex", enable in Share Sheet. Done — share any tweet/page,
   optionally add a why, and it lands in the inbox within seconds.

After building it, share the shortcut as an iCloud link and paste it below for
click-to-add on future devices.

iCloud link (click-to-add): _(pin your own here — NEVER in a public repo: shared shortcuts embed your PAT)_