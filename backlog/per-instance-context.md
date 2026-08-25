# Per-instance context instructions (beyond scope)

Some instances need
more than a scope list: standing context that steers scanning, enrichment,
and digestion (e.g. which link shapes are noise here, what the community's
shorthand means, what depth a domain deserves). To design: a designated
content file the skills consult when present (not README — that's
human-facing and already multi-purpose), offered as an option during
setup/ingest, shipped as an empty slot in the template. Hard constraint:
content only — machinery stays identical across instances; most instances
never fill it.
