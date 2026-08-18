# "Send to Dex" — one-tap capture from iPhone/iPad

Share any link from any app → it lands in your volume's inbox, ready for the next
ingest. One shortcut works for one volume or many: with a single volume it never
asks; with several it shows a picker.

## One-time setup (~5 minutes)

**Token**: GitHub → Settings → Developer settings → Fine-grained tokens → new
token. Repository access: only your volume repo(s). Permissions: Contents R/W +
Issues R/W. (Fine-grained tokens are per-owner — all repos in one shortcut must
share an owner.)

**Shortcut** (Shortcuts app → new shortcut):

1. **Receive** input from Share Sheet (URLs, Text). If there's no input: Stop.
2. **Dictionary** — one row per volume: key = display name, value = `owner/repo`.
   e.g. `Engineering → you/dex-engineering`, `Marketing → you/dex-marketing`.
3. **Text** — paste your PAT. → **Set Variable** `token`.
4. **Get Dictionary Keys** (from the Dictionary) → **Count** (Items).
5. **If** Count = 1:
   - **Get Item from List** (First Item, of the keys) → **Get Dictionary Value**
     for it → **Set Variable** `repo`.
   **Otherwise**:
   - **Choose from List** (the keys, prompt "Which dex?") → **Get Dictionary
     Value** for Chosen Item → **Set Variable** `repo`.
   **End If**.
6. **Ask for Input** (Text), prompt "Why? (optional)", allow empty.
7. **Get Contents of URL**:
   - URL: `https://api.github.com/repos/` `repo` `/issues`  (insert the variable inline)
   - Method: POST
   - Headers: `Authorization` = `Bearer ` + `token` · `Accept` = `application/vnd.github+json`
   - Request Body: **JSON**, two text fields: `title` = Shortcut Input,
     `body` = Provided Input

Name it "Send to Dex", enable in the Share Sheet. Done: share → (pick volume) →
optional why → filed. A single-volume dictionary never asks anything.

## Notes

- Success = an issue appears on the repo, gets auto-filed to `state/inbox.md`, and
  closes itself. Set the repo's Watch to **Ignore** to silence the traffic.
- **Never share your shortcut or its iCloud link** — shared shortcuts embed the PAT.
  Share this recipe (or a placeholder-token copy) instead.
- Any HTTP client works the same way: POST an issue with `title` = URL. Bookmarklets
  and CLI aliases are equally valid capture clients.
