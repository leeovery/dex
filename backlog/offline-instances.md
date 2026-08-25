# Offline instances can't run bin/dex at all

Measured during the
pipeline rewrite (2026-08-20, uv 0.12.5): uvx re-resolves the git ref on
every invocation (good: pin bumps take effect immediately, no cache
staleness) but fails hard when the source is unreachable, even with a
fully cached env. A laptop on a plane can't lint or capture-materialize.
To consider: a shim fallback to the cached env when resolution fails,
or `uv tool install` as an offline-capable channel.
