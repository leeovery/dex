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

Open the Shortcuts app and create a new shortcut. Add the actions below in
order by searching the actions panel; the Share Sheet wiring comes last.
1. Add **Dictionary**. Add one row per volume — the key is a display name, the
   value is the repo path. For example:
   - `Engineering` → `you/dex-engineering`
   - `Marketing` → `you/dex-marketing`
2. Add **Text**, paste your token into it, then add **Set Variable** and name the
   variable `token`.
3. Add **Get Dictionary Keys**, then add **Count** (set to count **Items**).
4. Add **If**, with the condition **Count is 1**:
   1. In the **If** branch: add **Get Item from List** (First Item, from the
      keys), then **Get Dictionary Value** for it, then **Set Variable** named
      `repo`.
   2. In the **Otherwise** branch: add **Choose from List** (over the keys, with
      the prompt "Which dex?"), then **Get Dictionary Value** for the Chosen
      Item, then **Set Variable** named `repo`.
5. Add **Ask for Input** (type **Text**), with the prompt "Why? (optional)".
   Leave the default answer empty.
6. Add **Get Contents of URL** and configure it:
   1. URL: type `https://api.github.com/repos/`, insert the **repo** variable,
      then type `/issues`.
   2. Tap the arrow to expand the options. Set **Method** to **POST**.
   3. Add two headers:
      - `Authorization`: type `Bearer `, then insert the **token** variable.
      - `Accept`: `application/vnd.github+json`
   4. Set **Request Body** to **JSON** and add two text fields:
      - `title`: insert **Shortcut Input**
      - `body`: insert **Provided Input**
7. Name the shortcut **Send to Dex** (tap the name at the top to rename it).
8. Turn on Share Sheet input. This is a shortcut setting, not an action — you
   won't find it by searching the actions panel.
   1. Dismiss the action-search panel if it's covering the bottom of the
      editor (swipe it down or tap outside it).
   2. Tap the details button at the bottom of the editor and turn on
      **Show in Share Sheet**, then tap **Done**.
   3. A **Receive [input] from Share Sheet** header appears above your first
      action. Tap its highlighted input-types chip.
   4. Turn on:
      - **Safari web pages**
      - **URLs**
      - **Articles**
      - **Text**
      - **Rich text**
      - **App Store apps**
   5. Turn everything else off — images, files, and PDFs share as data, not
      links, and this capture path can't carry them.
   6. Set **If there's no input** to **Stop and Respond**.

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
