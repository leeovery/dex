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

Open the Shortcuts app and create a new shortcut. Work through the sections
below in order. Actions are added by searching the actions panel, except the
Share Sheet setting at the end.

Two things to know about how Shortcuts works before you start:

- A shortcut is a chain: each action automatically passes its result to the
  action below it. When these instructions say to add several actions in a
  row, that automatic hand-off is doing the wiring — you rarely connect
  anything by hand.
- **Set Variable** is how you save a result for use later, further down the
  chain. It's an ordinary action: it takes whatever the action above produced
  and gives it a name you choose.
- Any field inside an action can also point at the result of an *earlier*
  action, not just the one directly above. Tap the field, choose
  **Select Variable**, then tap the result bubble beneath the action you want.
  These instructions call that "point it at ...".

### List your volumes

The dictionary is the shortcut's list of knowledge bases. It's what lets one
shortcut serve any number of volumes, and it's the only thing you edit when you
add a volume later.

1. Add a **Dictionary** action.
2. For each volume, tap **Add new item** and:
   1. Choose **Text** as the item type. (Number, Array, Dictionary, and Boolean
      aren't used here.)
   2. Set the key to the volume's display name.
   3. Set the text value to the volume's repo path.

For example:

- key `Engineering`, text `you/dex-engineering`
- key `Marketing`, text `you/dex-marketing`

### Store the token

The shortcut proves who it is to GitHub with the token you created earlier.
You'll store the token in one place and give it a name, so the final step can
use it — and so there's exactly one box to update if you ever replace the
token.

1. Search the actions panel for "text" and add the **Text** action. It appears
   as an empty text box — paste your token into it.
2. Search for "set variable" and add the **Set Variable** action directly
   below. This is a second, separate action: it takes the result of the action
   above it (your token) and saves it under a name.
3. In the Set Variable action, tap **Variable Name** and type `token`. Leave
   its input alone — Shortcuts has already connected it to the Text box above.

### Route to the right volume

These actions pick the destination volume — and skip the picker entirely when
your dictionary has only one volume in it.

1. Search the actions panel for "dictionary value" and add
   **Get Dictionary Value**. (There is no separate "get keys" action — this one
   does it, once you change its mode.)
2. In that action, tap the highlighted word **Value** and change it to
   **All Keys**. It now produces the list of your volume names.
3. Check its dictionary field: Shortcuts may have auto-filled it with the token
   text from the action above. Tap the field and point it at your
   **Dictionary** from the first section.
4. Search for "count" and add **Count**. Leave it counting **Items** — it
   connects itself to the list of names above.
5. Search for "if" and add **If**. Set its condition to **is** and the number
   to **1**. Everything you add next goes inside one of its two branches.
6. Inside the **If** branch (one volume — use it without asking):
   1. Add **Get Dictionary Value**. Change **Value** to **All Values** and
      point its dictionary field at your **Dictionary**. With one volume, the
      "list" of values is just that volume's repo path.
   2. Add **Set Variable** below it, and name the variable `repo`.
7. Inside the **Otherwise** branch (several volumes — ask which):
   1. Add **Choose from List**. Point its list field at the **All Keys** result
      from step 2, and set the prompt to "Which dex?".
   2. Add **Get Dictionary Value** (leave its mode as **Value**). Point its
      dictionary field at your **Dictionary**, and in its key field insert
      **Chosen Item**.
   3. Add **Set Variable** below it, and name the variable `repo`.

Whichever branch runs, the shortcut now holds the destination repo path in the
`repo` variable.

### Capture the "why"

A one-line note about why something caught your eye is often the most valuable
part of a capture — it travels with the link into the knowledge base.

1. Add **Ask for Input**, type **Text**, with the prompt "Why? (optional)".
2. Leave the default answer empty.

### File it

This step sends the capture: it opens a GitHub issue on the chosen volume,
which the volume's workflow files into its inbox and then closes.

1. Add **Get Contents of URL**.
2. In the URL field: type `https://api.github.com/repos/`, insert the **repo**
   variable, then type `/issues`.
3. Tap the arrow to expand the options and set **Method** to **POST**.
4. Add two headers:
   - `Authorization`: type `Bearer `, then insert the **token** variable.
   - `Accept`: `application/vnd.github+json`
5. Set **Request Body** to **JSON** and add two text fields:
   - `title`: insert **Shortcut Input**
   - `body`: insert **Provided Input**

### Name it and add it to the Share Sheet

This makes the shortcut appear when you tap Share in other apps, and defines
what kinds of shares it accepts. It's a shortcut setting, not an action — you
won't find it by searching the actions panel.

1. Tap the name at the top and rename the shortcut to **Send to Dex**.
2. Dismiss the action-search panel if it's covering the bottom of the editor
   (swipe it down or tap outside it).
3. Tap the details button at the bottom of the editor, turn on
   **Show in Share Sheet**, then tap **Done**.
4. A **Receive [input] from Share Sheet** header appears above your first
   action. Tap its highlighted input-types chip and turn on:
   - **Safari web pages**
   - **URLs**
   - **Articles**
   - **Text**
   - **Rich text**
   - **App Store apps**
5. Turn everything else off. Images, files, and PDFs share as data, not links —
   this capture path can't carry them.
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
