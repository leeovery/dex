# Capture clients beyond the shortcut

Push clients — the ones a human deliberately hands something to. The
counterpart to `watchers.md`, which is about sources that get polled. Both
end at the same artifact, one `.md` in `inbox/`, and the protocol is already
written down, so each of these is a small independent build rather than a
change to anything.

Candidates, roughly by value over effort:

- **Email.** A forwarding address landing in a small hosted endpoint that
  performs the contents-API PUT. Every device and every app on earth can
  already send email, so it is the one capture route that needs nothing
  installed anywhere — and it is the only route that reaches newsletters,
  which currently cannot be captured at all: they arrive in an inbox, have no
  shareable URL worth keeping, and die there.
- **Bookmarklet.** An evening's work, and it covers desktop browsers and iOS
  Safari without an install or a review process. Ships before any extension.
- **Browser extension.** What the bookmarklet cannot do: a proper note field,
  right-click on an image or a selection, a keyboard shortcut.
- **Chat bot** (Telegram or similar). Forward-to-save with no token entry and
  no install — the shape a non-technical owner would actually use.
- **Launcher extension** (Raycast, Alfred). Near-zero build, aimed squarely at
  the owner already living in a launcher.
- **Voice.** Record and capture as a binary; the transcription capability is
  already in the engine and already has a free local floor. The capture that
  works while walking away from a conversation.

**A hosted capture endpoint is a client, not machinery.** The rule that an
instance runs no server-side machinery governs the instance's data path — no
robot writes into a knowledge base. A relay that turns an email or a chat
message into the same authenticated contents-API PUT a phone would have made
is a capture client that happens to run on a server. Worth stating in the
capture protocol doc when the first one is built, so it is not relitigated.

Each of these needs a token or an identity. That is the same friction the
shortcut has, and the answer may be shared — see
`shortcut-per-instance-tokens.md`.
