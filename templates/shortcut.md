# Send To Dex — one-tap capture from iPhone or iPad

Share a link from any app to file it in your volume's inbox, ready for the next
ingest. One shortcut handles any number of volumes: with one volume it files
silently; with several it shows a picker.

## Before you begin

Create a token that the shortcut uses to file captures:

1. On GitHub, go to **Settings > Developer settings > Fine-grained tokens** and
   tap **Generate new token**.
2. Name the token so future-you knows what it is — for example `send-to-dex` —
   and add a description like "Files captures from my phone into dex volume
   inboxes. Lives inside the Send To Dex shortcut."
3. Set **Expiration** to **No expiration**. The shortcut breaks silently the day
   a token expires; if you prefer an expiring token, set a calendar reminder to
   rotate it.
4. Under **Repository access**, select only your volume repo (or repos).
5. Under **Permissions**, set **Contents** and **Issues** to **Read and write**.
6. Generate the token and copy it.

Fine-grained tokens work for one account or organization at a time. Every repo
in one shortcut must belong to the same owner.

## Create the shortcut

Open the Shortcuts app and create a new shortcut, then work through the
sections below in order.

Terms used throughout:

- An **action** is one building block of a shortcut — a box that does one
  thing. When a step says **add** an action, it means: search the actions
  panel for the action's name, then tap it to add it. The one exception is
  the Share Sheet setting at the end, which isn't an action.

And three things to know about how Shortcuts behaves:

- A shortcut is a chain: each action automatically passes its result to the
  action below it. When these instructions add several actions in a row, that
  automatic hand-off does the wiring.
- **Set Variable** saves a result under a name so a later step can use it. It's
  an ordinary action: it takes whatever the action above produced and names it.
- Shortcuts sometimes connects an action to the wrong earlier result. To
  repoint it: tap the blue variable chip, tap **Clear** (the panel closes and
  the field shows a faded placeholder), then tap the faded placeholder and a
  bar of options appears. If the result you need is listed in the bar, pick
  it. If it isn't, tap **Select Variable** — the editor switches to a view
  with a blue token beneath every action — and tap the token beneath the
  action whose result you want. These instructions call that
  "repoint it to ...".

### Step 1: List your volumes

The dictionary is the shortcut's list of knowledge bases. It's what lets one
shortcut serve any number of volumes, and it's the only thing you edit when you
add a volume later.

1. Add **Dictionary**.
2. For each volume, tap **Add new item**, then:
   1. Choose **Text** as the item type. (Number, Array, Dictionary, and Boolean
      aren't used here.)
   2. Set the key to the volume's display name.
   3. Set the text value to the volume's repo path.

For example:

- key `Engineering`, text `you/dex-engineering`
- key `Marketing`, text `you/dex-marketing`

### Step 2: Store the token

The shortcut proves who it is to GitHub with the token you created earlier.
You'll store the token in one place and give it a name, so the final step can
use it — and so there's exactly one box to update if you ever replace the
token.

1. Add **Text**. It appears as an empty
   text box — paste your token into it.
2. Add **Set Variable** directly
   below. This is a second, separate action: it takes the result of the action
   above it (your token) and saves it under a name.
3. In the Set Variable action, tap **Variable Name** and type `token`. Leave
   its input alone — Shortcuts has already connected it to the Text box above.

### Step 3: Route to the right volume

These actions pick the destination volume — and skip the picker entirely when
your dictionary has only one volume in it.

1. Add **Get Dictionary Value**.
2. In that action, tap the highlighted word **Value** and change it to
   **All Keys**.
3. The action now reads "Get All Keys in `token`". Shortcuts has guessed the
   wrong source — it should read your volume list, not your token. Fix it:
   1. Tap the blue `token` chip, then tap **Clear**. The panel closes and the
      field shows a faded **Dictionary** placeholder.
   2. Tap the faded placeholder. A bar appears listing the workflow's results
      by name — for example `token`, `Dictionary`, `Text`.
   3. Pick `Dictionary`.
   The action now reads "Get All Keys in `Dictionary`" — that's correct. It
   produces the list of your volume names.
4. Add **Count**. It arrives reading
   "Count Items in `Dictionary Value`" — that's correct: `Dictionary Value` is
   the list of names produced by the previous step. Leave it as is.
5. Add **If**. This adds three
   connected rows at once — **If**, **Otherwise**, and **End If** — which
   form two branches: actions placed between **If** and **Otherwise** run
   when the condition is true; actions between **Otherwise** and **End If**
   run when it isn't.
6. The **If** row arrives reading "If `Count` is **Number**", with **Number**
   faded — `Count` and **is** are already correct by default. Tap the faded
   **Number**, enter **1**, and close the panel. The row now reads
   "If `Count` is 1 **+**" — ignore the **+**; it adds extra conditions to the
   check, which we don't need.
7. Get each of the next actions into the right branch. New actions land at
   the bottom of the shortcut, not inside the If block — for each one, touch
   and hold it, then drag it up until it sits indented directly beneath the
   row it belongs under (**If** for steps 8, **Otherwise** for steps 9).
8. Between **If** and **Otherwise** (one volume — use it without asking):
   1. Add **Get Dictionary Value**. Change **Value** to **All Values**, and if its
      source isn't `Dictionary`, repoint it to `Dictionary`. It should read
      "Get All Values in `Dictionary`".
   2. Add **Set Variable**
      below it. Tap **Variable Name** and type `repo`. The row now reads
      "Set `repo` to `Dictionary Value`" — that's correct: it saves the repo
      path under the name `repo`.
9. Between **Otherwise** and **End If** (several volumes — ask which):
   1. Add **Choose from List** and drag it beneath **Otherwise**. It arrives
      reading "Choose from `Count`" — the wrong source. Fix it:
      1. Tap the `Count` chip, then tap **Clear**. The row now shows a faded
         **Choose** placeholder.
      2. Tap the faded placeholder. The bar that appears won't list what we
         need, so tap **Select Variable**.
      3. In the selection view, tap the blue token beneath your first
         **Get Dictionary Value** action (the one reading "Get All Keys in
         `Dictionary`").
      The row now reads "Choose from `Dictionary Value`". Then tap the arrow
      on the action and set **Prompt** to "Which dex?".
   2. Add **Get Dictionary Value** and drag it beneath **Choose from List**.
      It arrives reading "Get Value for **key** in `Selected Item`" — the
      key is empty and the source is wrong. Fix both:
      1. Tap the `Selected Item` chip (after "in"), then tap **Clear**. Tap
         the faded placeholder — a panel slides up listing all the workflow's
         results — and pick `Dictionary`.
      2. Tap the faded **key** field — a small, horizontally scrollable bar
         appears at the bottom of the screen — and pick `Selected Item`,
         scrolling if needed.
      The row now reads "Get Value for `Selected Item` in `Dictionary`".
   3. Add **Set Variable**
      below it. Tap **Variable Name** and type `repo`. As in the other branch,
      the row now reads "Set `repo` to `Dictionary Value`".

Whichever branch runs, the shortcut now holds the destination repo path in the
`repo` variable.

### Step 4: Capture the "why"

A one-line note about why something caught your eye is often the most valuable
part of a capture — it travels with the link into the knowledge base.

1. Add **Ask for Input**.
2. Set its type to **Text** and its prompt to "Why? (optional)". Leave the
   default answer empty.

### Step 5: File it

This step sends the capture: it opens a GitHub issue on the chosen volume,
which the volume's workflow files into its inbox and then closes.

1. Add **Get Contents of URL**. It arrives reading "Get contents of" with the
   Ask for Input result already filled in — the wrong content for this field.
   Tap that chip, then tap **Clear**. The row now reads
   "Get contents of **URL**" with **URL** faded.
2. Tap the faded **URL** field and type `https://api.github.com/repos/`, pick
   `repo` from the bar at the bottom of the screen, then type `/issues`.
3. Tap the arrow to expand the options and set **Method** to **POST**.
4. Add two headers. Each header is a pair of fields — a key and a text value —
   and no colons are typed anywhere:
   1. Tap **Add new header**. In the key field type `Authorization`. In the
      text field type `Bearer`, then a space, then pick `token` from the bar
      at the bottom of the screen.
   2. Tap **Add new header** again. In the key field type `Accept`. In the
      text field type `application/vnd.github+json`.
5. Set **Request Body** to **JSON** and add two fields, the same key/text
   pattern as the headers:
   1. Tap **Add new field** and choose **Text**. In the key field type
      `title`. In the text field, pick `Shortcut Input` from the bar at the
      bottom of the screen.
   2. Tap **Add new field** and choose **Text** again. In the key field type
      `body`. In the text field, pick `Ask for Input` from the bar — that's
      the answer you typed at the "Why? (optional)" prompt.

### Step 6: Name it and add it to the Share Sheet

This makes the shortcut appear when you tap Share in other apps, and defines
what kinds of shares it accepts. It's a shortcut setting, not an action — you
won't find it by searching the actions panel.

1. Tap the name at the top and rename the shortcut to **Send To Dex**.
2. Dismiss the actions panel if it's covering the bottom of the editor (swipe
   it down or tap outside it).
3. Tap the small info button (an ⓘ in a circle) at the bottom of the editor,
   turn on **Show in Share Sheet**, then tap the blue tick in the top-right
   corner.
4. Scroll back to the top of the action list, above your Dictionary. A new
   action has appeared there reading "Receive **Apps and 18 more** from
   **Share Sheet**. If there's no input: **Continue**". Tap the highlighted
   **Apps and 18 more** chip.
5. Everything is toggled on by default. Turn OFF the following and leave
   everything else on:
   - **Images**
   - **Media**
   - **Files**
   - **Folders**
   - **PDFs**
   - **Map links**
   - **Locations**
   - **Contacts**
   - **Email addresses**
   - **Phone numbers**
   - **Dates**
   - **iTunes products**
6. Tap the blue tick in the top-right corner to confirm the toggles.
7. Tap **Continue** (after "If there's no input:") and change it to
   **Stop and Respond**. It asks for a response — the message shown if the
   shortcut is ever run without anything shared. Enter:
   `Nothing to send — share a link to use Send To Dex.`

### Optional: make it shareable with import questions

**Import Questions** (in the same ⓘ info panel) let a shared copy ask its new
owner for their own values at import time. A field that has an import question
is cleared automatically when the shortcut is shared — so your token never
travels with it.

1. In the info panel, tap **Import Questions**, then **Add New Question**.
2. Choose the token **Text** parameter from the list, tap its name in the
   Import Questions area, and set the **Question Text** to something like
   "Paste your GitHub token (see Before you begin in the setup guide)".
   Confirm, then tap **Done**.
3. Set your Dictionary to example rows before sharing, or add import questions
   for its values the same way — repo paths aren't secret, and importers can
   edit them after import either way.
4. Share as an iCloud link. The token field arrives blank and the importer is
   prompted for their own.

## Try it

Share any page from Safari. Within about 30 seconds, an issue appears on the
volume repo, the inbox workflow files it into `state/inbox.md`, and the issue
closes itself.

## Notes

- To silence GitHub's issue notifications, set the volume repo's **Watch**
  setting to **Ignore**.
- Never share a shortcut whose token field lacks an import question — shared
  shortcuts embed whatever is in their fields. With the import question set
  (above), the token field is cleared on share and sharing is safe.
- Text shares work for short snippets. GitHub caps issue titles at about 256
  characters, so long text fails — share a link to the source instead.
- Any HTTP client can capture the same way: POST an issue with the URL as the
  title. A bookmarklet or a CLI alias works as well as a shortcut.
