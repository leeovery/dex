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
- A shortcut is a chain: each action automatically passes its result to the
  action below it. When these instructions add several actions in a row, that
  automatic hand-off does the wiring.
- **Add Set Variable named `x`** means: add the **Set Variable** action, tap
  **Variable Name**, and type `x`. Its input is whatever the action above
  produced, unless a step says to repoint it.
- **Repoint it to `x`** means: tap the blue variable chip, tap **Clear** (the
  panel closes and the field shows a faded placeholder), then tap the faded
  placeholder and pick `x` from the bar that appears. If the bar doesn't list
  it, tap **Select Variable** — the editor switches to a view with a blue
  token beneath every action — and tap the token beneath the action whose
  result you want.
- **Place the next actions between two rows** (for example between **If**
  and **Otherwise**): new actions land at the bottom of the shortcut —
  touch and hold each one and drag it up until it sits indented directly
  beneath the row it belongs under.
- Each step ends with what the finished row reads. If your row reads
  differently, fix it before moving on.

### Step 1: List your instances

The dictionary lists your instances. It's the only thing you edit when you
add an instance later.

1. Add **Dictionary**.
2. For each instance, tap **Add new item**, then:
   1. Choose **Text** as the item type.
   2. Set the key to the instance's display name.
   3. Set the text value to the instance's repo path.

For example:

- key = `Engineering`, text = `you/dex-engineering`
- key = `Marketing`, text = `you/dex-marketing`

### Step 2: Store the token

Store the token once, under a name, so later steps can use it.

1. Add **Text**. It appears as an empty text box — paste your token into it.
2. Add **Set Variable** below it, named `token`. The row reads
   "Set variable `token` to `Text`".

### Step 3: Route to the right instance

These actions pick the destination instance — and skip the picker entirely
when your dictionary has only one instance in it.

1. Add **Get Dictionary Value**. Tap the highlighted word **Value** and change
   it to **All Keys**, then repoint its source to `Dictionary`. The row reads
   "Get All Keys in `Dictionary`".
2. Add **Count**. The row reads "Count Items in `Dictionary Value`".
3. Add **If**. This adds three connected rows at once — **If**, **Otherwise**,
   and **End If**: actions between **If** and **Otherwise** run when the
   condition is true; actions between **Otherwise** and **End If** run when it
   isn't. Tap the faded **Number** and enter **1**, ignoring the **+**
   after it. The row reads "If `Count` is 1".

Place the next actions between **If** and **Otherwise**. They run when there
is one instance — use it without asking:

4. Add **Get Dictionary Value**. Change **Value**
   to **All Values**, and repoint its source to `Dictionary` if needed. The
   row reads "Get All Values in `Dictionary`".
5. Add **Set Variable** below it, named `repo`. The row reads
   "Set variable `repo` to `Dictionary Value`".

Place the next actions between **Otherwise** and **End If**. They run when
there are several instances — show a picker:

6. Add **Choose from List**. Repoint its
   input to the result of step 1 (use **Select Variable** and tap the token
   beneath "Get All Keys in `Dictionary`"). Tap the arrow on the action and
   set **Prompt** to `Which dex?`. The row reads
   "Choose from `Dictionary Value`".
7. Add **Get Dictionary Value** below it. Repoint its source (after "in") to
   `Dictionary`. Tap the faded **key** field and pick `Selected Item` from
   the horizontally scrollable bar at the bottom of the screen. The row reads
   "Get Value for `Selected Item` in `Dictionary`".
8. Add **Set Variable** below it, named `repo`. The row reads
   "Set variable `repo` to `Dictionary Value`".

### Step 4: Capture the "why"

This step asks for an optional note with each capture.

1. Add **Ask for Input**. Set its type to **Text** and its prompt to
   `Why? (optional)`. Leave the default answer empty. The row reads
   "Ask for **Text** with "Why? (optional)"".

### Step 5: File it

These actions write the capture into the instance repo.

1. Add **Date**. The row reads "Current Date".
2. Add **Format Date**. Set **Date Format** to **Custom** and the format
   string to `yyyyMMdd-HHmmss`. The row reads "Format `Date`".
3. Add **Set Variable** below it, named `stamp`. The row reads
   "Set variable `stamp` to `Formatted Date`".
4. Add **Get Images from Input**. Repoint it to `Shortcut Input`. The row
   reads "Get images from `Shortcut Input`".
5. Add **Count**. Repoint it to `Images` if it connected to anything else.
   The row reads "Count Items in `Images`".
6. Add **If**, with the condition **is greater than** and the number **0**.
   The row reads "If `Count` is greater than 0".

Place the next actions between **If** and **Otherwise**. They run when an
image was shared:

7. Add **Resize Image**. Repoint it to `Images`
   if needed. Set the width to `2048` and leave the height on **Auto
   Height**. The row reads "Resize `Images`".
8. Add **Convert Image** below it, converting to **JPEG**. The row reads
   "Convert `Resized Image` to **JPEG**".
9. Add **Set Variable** below it, named `blob`. The row reads
   "Set variable `blob` to `Converted Image`".
10. Add **Text** below that, containing exactly `.jpg`.
11. Add **Set Variable** below it, named `ext`. The row reads
    "Set variable `ext` to `Text`".

Place the next actions between **Otherwise** and **End If**. They run when
the share isn't an image:

12. Add **Get URLs from Input**. Repoint
    it to `Shortcut Input`. The row reads "Get URLs from `Shortcut Input`".
13. Add **Count** below it. Repoint it to `URLs` if it connected to
    anything else. The row reads "Count Items in `URLs`".
14. Add **If** below it. Tap the faded **Number** and enter **0**. Delete its
    **Otherwise** row. This If sits nested inside the outer **Otherwise**.
    The row reads "If `Count` is 0".

    Place the next actions inside it. They run when no URL was shared:

    1. Add **Get Details of Files**. Set the detail to **File Extension**,
       and repoint its input to `Shortcut Input`. The row reads
       "Get **File Extension** of `Shortcut Input`".
    2. Add **If** below it, with the condition **has any value**. Delete its
       **Otherwise** row too. The row reads
       "If `File Extension` has any value".

       Place the next actions inside it. They run when a file was shared:

       1. Add **Set Variable** named `blob`, repointing its input to
          `Shortcut Input`. The row reads
          "Set variable `blob` to `Shortcut Input`".
       2. Add **Text** below that, containing `.` immediately followed by
          the **File Extension** variable.
       3. Add **Set Variable** below it, named `ext`. The row reads
          "Set variable `ext` to `Text`".

    The bottom of the shortcut now reads three **End If** rows in a row —
    the two nested ones, then the outer one, which returns to the top level.

Below the outer **End If**:

15. Add **If**. Repoint its input to `blob`, and set the condition to
    **has any value**. The row reads "If `blob` has any value".

    Place the next actions between **If** and **Otherwise**. They run when a
    binary was shared:

    1. Add **Get Contents of URL**. Configure:
       1. URL: `https://api.github.com/repos/` + the `repo` variable +
          `/releases/tags/inbox`
       2. Method: **GET**
       3. Headers: key = `Authorization`, text = `Bearer` + space + the
          `token` variable
    2. Add **Get Dictionary Value** below it. Type `id` into the key field;
       its source is `Contents of URL` (repoint if it connected elsewhere).
       The row reads "Get Value for `id` in `Contents of URL`".
    3. Add **Set Variable** below it, named `release`. The row reads
       "Set variable `release` to `Dictionary Value`".
    4. Add **Get Contents of URL** below that. Configure:
       1. URL: `https://uploads.github.com/repos/` + the `repo` variable +
          `/releases/` + the `release` variable + `/assets?name=` + the
          `stamp` variable + the `ext` variable
       2. Method: **POST**
       3. Headers:
          - key = `Authorization`, text = `Bearer` + space + the `token` variable
          - key = `Content-Type`, text = `application/octet-stream`
       4. Request Body: **File**, set to the `blob` variable.
    5. Add **Get Dictionary Value** below it. Type `url` into the key field;
       its source is `Contents of URL` (the POST directly above). The row
       reads "Get Value for `url` in `Contents of URL`".
    6. Add **Set Variable** below it, named `asset`. The row reads
       "Set variable `asset` to `Dictionary Value`".
    7. Add **Text** below that, containing exactly five lines then your
       note:

       ```
       ---
       asset: [asset]
       name: [stamp][ext]
       ---

       [Ask for Input]
       ```

       Each bracketed name is that variable; type the surrounding characters
       literally. `[stamp][ext]` sit together with no space.
    8. Add **Set Variable** below it, named `payload`. The row reads
       "Set variable `payload` to `Text`".

    Place the next actions between **Otherwise** and **End If**. They run
    when a link or text was shared:

    9. Add **Text**. First line: the `Shortcut Input` variable. Then an
       empty line, then the `Ask for Input` variable.
    10. Add **Set Variable** below it, named `payload`. The row reads
        "Set variable `payload` to `Text`".

Below that **End If**, at the top level of the shortcut:

16. Add **Base64 Encode**. Repoint its input to `payload`. Expand its
    options and set **Line Breaks** to **None**. The row reads
    "Base64 Encode `payload`".
17. Add **Get Contents of URL** below it. Configure:
    1. URL: `https://api.github.com/repos/` + the `repo` variable +
       `/contents/inbox/` + the `stamp` variable + `.md`
    2. Method: **PUT**
    3. Headers:
       - key = `Authorization`, text = `Bearer` + space + the `token` variable
       - key = `Accept`, text = `application/vnd.github+json`
    4. Request Body **JSON**, two text fields:
       - key = `message`, text = the literal word `capture`
       - key = `content`, text = the `Base64 Encoded` variable (the action
         above)

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
   everything else on (**Images and Files stay on** — image and file capture are supported):
   - **Media**
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
instance repo adding a `.md` file under `inbox/` — that's the capture,
with your note in its body, waiting for the next ingest. Share a photo to see
the binary path: the resized image appears as an asset on the repo's **inbox**
release, and the committed `.md` points at it. The next ingest moves the
binary into the repo (under `media/`, LFS-tracked) and deletes the asset.

## Notes

- Never share a shortcut whose token field lacks an import question — shared
  shortcuts embed whatever is in their fields. With the import question set
  (above), the token field is cleared on share and sharing is safe.
- The shortcut is one client of the capture protocol (`docs/capture.md`) —
  any HTTP client can capture the same way: a bookmarklet or CLI alias works
  as well as a shortcut.
