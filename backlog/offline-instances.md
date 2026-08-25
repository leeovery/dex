# Offline instances can't run bin/dex at all

Measured during the
pipeline rewrite (2026-08-20, uv 0.12.5): uvx re-resolves the git ref on
every invocation (good: pin bumps take effect immediately, no cache
staleness) but fails hard when the source is unreachable, even with a
fully cached env. A laptop on a plane can't lint or capture-materialize.
To consider: a shim fallback to the cached env when resolution fails,
or `uv tool install` as an offline-capable channel.

A closed question, recorded so it is not reopened: rewriting the engine in Go
or Rust was considered on 2026-08-21 and rejected. The dependencies are the
engine — yt-dlp, faster-whisper, trafilatura, the document extractor — and a
rewrite would shell out to most of them anyway while losing the rest. Every
real complaint about Python here is a distribution complaint, and this entry
is where they are answered. If anything goes native it is the edge clients,
and those are TypeScript.
