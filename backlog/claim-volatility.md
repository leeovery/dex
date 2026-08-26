# Claim volatility

A wiki page carries one date for the whole page and dates on individual
claims, but nothing says how fast a claim decays. Those rates differ by an
order of magnitude and the page cannot tell a reader which is which.

"Entity resolution dominates retrieval accuracy" is a finding about how
systems behave; it ages over years. "LLMs cannot reliably write Cypher, so
expose templated queries instead" is a claim about model capability from
2024-10, and model capability moves in months — by 2026 it may simply be
false. Both sit in the same page, both correctly dated, and a reader
following the recency rule treats them alike.

Observed 2026-08-26: a session consulting dex-engineering surfaced the
Cypher claim as current guidance while designing a system. The page was not
wrong — the claim lives under "Notable positions worth preserving", not
"Current state" — but nothing in the citation carried that distinction once
it was quoted, and the section a claim came from is exactly what gets lost
when knowledge is repeated elsewhere.

To design: a volatility class per claim (something like durable / dated /
perishable), assigned when the claim is written and cheap for the writer to
judge — "is this about how systems behave, or about what a model can
currently do?" Then the consequences: the query skill states the class when
quoting a perishable claim; the health check re-verifies perishable claims
first rather than treating all stale pages alike; and a perishable claim past
some age is surfaced for re-checking rather than quietly retained.

Open: whether this is a frontmatter field, an inline marker, or purely a
convention in how sections are named; whether it applies to items as well as
claims; and whether an LLM can assign it consistently enough to be worth
having.
