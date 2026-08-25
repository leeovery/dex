# Resurfacing and the owner's reading queue

The corpus tracks machine
ingestion but not owner engagement: saved things vanish from mind exactly
like bookmarks and screenshot folders did. To design: (1) an owner
read/intent flag per item as derived state — fed by the capture note
("read later", "try on project X") and a presumed-unread default for
substantive items, cleared conversationally; (2) resurfacing views over it:
index leads with "new this week" / "waiting for you", query answers end
with related-but-unread items, and a periodic digest page composed by the
scheduled session (rides the scheduled-ingestion design). Recall queries ("what did I share about X", "what came
in last month") answer from corpus+digests — make that an explicit mode in
dex-query. Principle: one store (the corpus); queue, digest, and index
sections are regenerable views. A TUI/newsletter delivery layer waits
until the digest proves what the owner actually wants to see.
