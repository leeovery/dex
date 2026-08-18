# Send To Dex — one-tap capture from iPhone or iPad

Share a link from any app to file it in your instance's inbox, ready for the next
ingest. One shortcut handles any number of instances: with one instance it files
silently; with several it shows a picker.

## Add the ready-made shortcut (recommended)

_The shareable link is being rebuilt for the current capture design (direct
capture files, image support). Until it's re-pinned here, build manually below._

## Before you begin

Create a token that the shortcut uses to file captures:

1. On GitHub, go to **Settings > Developer settings > Fine-grained tokens** and
   tap **Generate new token**.
2. Name the token so future-you knows what it is — for example `send-to-dex` —
   and add a description like "Files captures from my phone into dex instance
   inboxes. Lives inside the Send To Dex shortcut."
3. Set **Expiration** to **No expiration**. The shortcut breaks silently the day
   a token expires; if you prefer an expiring token, set a calendar reminder to
   rotate it.
4. Under **Repository access**, select only your instance repo (or repos).
5. Under **Permissions**, set **Contents** to **Read and write**.
6. Generate the token and copy it.

Fine-grained tokens work for one account or organization at a time. Every repo
in one shortcut must belong to the same owner.

**If your instance repo is owned by an organization** (not your personal
account): fine-grained tokens hit two org policies — the org must be chosen as
the token's Resource owner, and org-owned tokens can't be non-expiring. The
practical alternative is a **classic** token (Tokens (classic), scope `repo`,
No expiration) — it works identically in this shortcut, at the cost of broader
access. The org must allow classic PAT access in its settings.

When you add a new dex instance later, also add its repo to this token's
**Repository access** — an upload to a repo the token can't reach fails
silently (GitHub returns a valid-looking 404, and the shortcut shows a
checkmark).

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

### Step 1: List your instances

The dictionary is the shortcut's list of knowledge bases. It's what lets one
shortcut serve any number of instances, and it's the only thing you edit when you
add an instance later.

1. Add **Dictionary**.
2. For each instance, tap **Add new item**, then:
   1. Choose **Text** as the item type. (Number, Array, Dictionary, and Boolean
      aren't used here.)
   2. Set the key to the instance's display name.
   3. Set the text value to the instance's repo path.

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

### Step 3: Route to the right instance

These actions pick the destination instance — and skip the picker entirely when
your dictionary has only one instance in it.

1. Add **Get Dictionary Value**.
2. In that action, tap the highlighted word **Value** and change it to
   **All Keys**.
3. The action now reads "Get All Keys in `token`". Shortcuts has guessed the
   wrong source — it should read your instance list, not your token. Fix it:
   1. Tap the blue `token` chip, then tap **Clear**. The panel closes and the
      field shows a faded **Dictionary** placeholder.
   2. Tap the faded placeholder. A bar appears listing the workflow's results
      by name — for example `token`, `Dictionary`, `Text`.
   3. Pick `Dictionary`.
   The action now reads "Get All Keys in `Dictionary`" — that's correct. It
   produces the list of your instance names.
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
8. Between **If** and **Otherwise** (one instance — use it without asking):
   1. Add **Get Dictionary Value**. Change **Value** to **All Values**, and if its
      source isn't `Dictionary`, repoint it to `Dictionary`. It should read
      "Get All Values in `Dictionary`".
   2. Add **Set Variable**
      below it. Tap **Variable Name** and type `repo`. The row now reads
      "Set `repo` to `Dictionary Value`" — that's correct: it saves the repo
      path under the name `repo`.
9. Between **Otherwise** and **End If** (several instances — ask which):
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

This step writes the capture into the instance repo: one new file per capture
in `state/inbox/`, created directly through GitHub's contents API. A link or
note becomes a small `.md` file; an image becomes a `.jpg`. The two branches
only prepare what to upload — a single upload action at the end sends it, and
your note travels in the commit message.

First, a timestamp for unique filenames:

1. Add **Date** (set to **Current Date**).
2. Add **Format Date**. Set its format to **Custom** and the format string to
   `yyyyMMdd-HHmmss`.
3. Add **Set Variable** below it. Tap **Variable Name** and type `stamp`.

Then detect whether an image was shared:

4. Add **Get Images from Input**. It arrives reading "Get images from
   `stamp`" — the wrong source. Repoint it: tap the `stamp` chip, tap
   **Clear**, tap the faded placeholder, and pick `Shortcut Input` from the
   options that appear.
5. Add **Count**. It should read "Count Items in `Images`" — `Images` is the
   result of Get Images from Input. If it connected to anything else, repoint
   it to `Images`.
6. Add **If**, with the condition **is greater than** and the number **0**.
   As in Step 3, drag each of the following actions into the right branch.

Between **If** and **Otherwise** (an image was shared):

7. Add **Convert Image**, converting to **JPEG**. If its input isn't `Images`,
   repoint it: tap its variable chip, tap **Clear**, tap the faded
   placeholder, and pick `Images` — via **Select Variable** and the token
   beneath **Get Images from Input** if the options don't list it.
8. Add **Base64 Encode** below it (it encodes the converted image
   automatically). Expand its options and set **Line Breaks** to **None** —
   the default inserts line breaks that GitHub rejects as invalid base64.
9. Add **Set Variable**, dragging it below the Base64 Encode. Tap
   **Variable Name** and type `payload`. The row now reads
   "Set `payload` to `Base64 Encoded`".
10. Add **Text**, dragging it below the Set Variable, containing exactly
    `.jpg`.
11. Add **Set Variable**, dragging it below that Text. Tap **Variable Name**
    and type `ext`. The row now reads "Set `ext` to `Text`".

Between **Otherwise** and **End If** (a link or text was shared):

12. Add **Text**, dragging it beneath **Otherwise**. First line: the
    `Shortcut Input` variable. Then an empty line, then the `Ask for Input`
    variable.
13. Add **Base64 Encode**, dragging it below that Text. Set its
    **Line Breaks** to **None**, as in step 8.
14. Add **Set Variable**, dragging it below the Base64 Encode. Tap
    **Variable Name** and type `payload`. The row now reads
    "Set `payload` to `Base64 Encoded`".
15. Add **Text**, dragging it below that, containing exactly `.md`.
16. Add **Set Variable**, dragging it below that Text. Tap **Variable Name**
    and type `ext`. The row now reads "Set `ext` to `Text`".

After **End If**, the single upload:

17. Add **Get Contents of URL**. Clear its auto-filled input as in earlier
    steps, then configure:
    1. URL: `https://api.github.com/repos/` + the `repo` variable +
       `/contents/state/inbox/` + the `stamp` variable + the `ext` variable
    2. Method: **PUT**
    3. Headers:
       - key `Authorization`, text `Bearer` + space + the `token` variable
       - key `Accept`, text `application/vnd.github+json`
    4. Request Body **JSON**, two text fields:
       - key `message`, text `capture ` + the `Ask for Input` variable — your
         "why" note becomes the commit message
       - key `content`, text = the `payload` variable

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
   everything else on (**Images stays on** — image capture is supported):
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
2. Working top to bottom: choose the **Dictionary** parameter (shown as
   "Dictionary items"). Set the **Question Text** to:
   `For each dex instance you have (one or more), add an item of type Text: key = its name (e.g. Engineering), text = its GitHub repo as owner/repo`
   Leave the **Default Answer** empty — the question text is the guidance.
   Confirm.
3. Tap **Add New Question** again, choose the token **Text** parameter, and set
   its **Question Text** to something like "Paste your GitHub token (see
   Before you begin in the setup guide)". Confirm, then tap **Done**.
4. Once the questions exist, **Change Answers** / **Customize Shortcut**
   becomes how ANY copy gets configured — including yours. It re-asks the
   questions and writes the answers into the shortcut's fields.
5. You can share directly from your configured copy: fields that carry import
   questions are cleared automatically in the shared artifact, so your token
   and instances never travel (verified — importing the shared link prompts
   for them). Each share produces a new link.
6. Share as an iCloud link. On import, the questions appear as a
   **Customize Shortcut** screen: for the instances question the importer taps
   **Add new item**, chooses **Text**, and fills the key (name) and text
   (owner/repo) — repeating for each instance — then pastes their token for
   the second question.

## Try it

Share any page from Safari. Within seconds a new commit appears on the
instance repo adding a file under `state/inbox/` — that's the capture, waiting
for the next ingest, with your note as the commit message. Share a photo to
see the image path: a `.jpg` lands the same way.

## Notes

- Never share a shortcut whose token field lacks an import question — shared
  shortcuts embed whatever is in their fields. With the import question set
  (above), the token field is cleared on share and sharing is safe.
- Any HTTP client can capture the same way: PUT a file into
  `state/inbox/` via the contents API. A bookmarklet or CLI alias works as
  well as a shortcut.
