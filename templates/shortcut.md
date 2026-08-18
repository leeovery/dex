# Send to Dex — one-tap capture from iPhone or iPad

Share a link from any app to file it in your volume's inbox, ready for the next
ingest. One shortcut handles any number of volumes: with one volume it files
silently; with several it shows a picker.

## Before you begin

Create a token that the shortcut uses to file captures:

1. On GitHub, go to **Settings > Developer settings > Fine-grained tokens** and
   tap **Generate new token**.
2. Name the token so future-you knows what it is — for example `send-to-dex` —
   and add a description like "Files captures from my phone into dex volume
   inboxes. Lives inside the Send to Dex shortcut."
3. Set **Expiration** to **No expiration**. The shortcut breaks silently the day
   a token expires; if you prefer an expiring token, set a calendar reminder to
   rotate it.
4. Under **Repository access**, select only your volume repo (or repos).
5. Under **Permissions**, set **Contents** and **Issues** to **Read and write**.
6. Generate the token and copy it.

Fine-grained tokens work for one account or organization at a time. Every repo in
one shortcut must belong to the same owner.

## Create the shortcut

Open the Shortcuts app and create a new shortcut.

1. Turn on Share Sheet input. This is a setting, not an action — it can't be
   found by searching the actions list.
   1. Tap the shortcut's settings (the info button, or tap the shortcut's name
      at the top and choose the settings icon).
   2. Turn on **Show in Share Sheet**, then close the settings.
   3. A **Receive [input] from Share Sheet** header now appears at the top of
      the editor. Tap its highlighted input-types chip.
   4. Turn on **Safari web pages**, **URLs**, **Articles**, **Text**,
      **Rich text**, and **App Store apps**.
   5. Turn everything else off. Images, files, and PDFs share as data, not
      links — this capture path can't carry them.
   6. Set **If there's no input** to **Stop and Respond**.
2. Add **Dictionary**. Add one row per volume: the key is a display name, the
   value is the repo path.
   For example: `Engineering` → `you/dex-engineering`, `Marketing` →
   `you/dex-marketing`.
3. Add **Text**, paste your token into it, then add **Set Variable** and name the
   variable `token`.
4. Add **Get Dictionary Keys**, then add **Count** (set to count **Items**).
5. Add **If**, with the condition **Count is 1**:
   1. In the **If** branch: add **Get Item from List** (First Item, from the
      keys), then **Get Dictionary Value** for it, then **Set Variable** named
      `repo`.
   2. In the **Otherwise** branch: add **Choose from List** (over the keys, with
      the prompt "Which dex?"), then **Get Dictionary Value** for the Chosen
      Item, then **Set Variable** named `repo`.
6. Add **Ask for Input** (type **Text**), with the prompt "Why? (optional)".
   Leave the default answer empty.
7. Add **Get Contents of URL** and configure it:
   1. URL: type `https://api.github.com/repos/`, insert the **repo** variable,
      then type `/issues`.
   2. Tap the arrow to expand the options. Set **Method** to **POST**.
   3. Add two headers:
      - `Authorization`: type `Bearer `, then insert the **token** variable.
      - `Accept`: `application/vnd.github+json`
   4. Set **Request Body** to **JSON** and add two text fields:
      - `title`: insert **Shortcut Input**
      - `body`: insert **Provided Input**
8. Name the shortcut **Send to Dex** and turn on **Show in Share Sheet**.

## Try it

Share any page from Safari. Within about 30 seconds, an issue appears on the
volume repo, the inbox workflow files it into `state/inbox.md`, and the issue
closes itself.

## Notes

- To silence GitHub's issue notifications, set the volume repo's **Watch**
  setting to **Ignore**.
- Don't share the shortcut or its iCloud link — shared shortcuts embed your
  token. Share this recipe instead, or a copy with a placeholder token.
- Text shares work for short snippets. GitHub caps issue titles at about 256
  characters, so long text fails — share a link to the source instead.
- Any HTTP client can capture the same way: POST an issue with the URL as the
  title. A bookmarklet or a CLI alias works as well as a shortcut.
