# Backfills (exports in raw/)

Getting exports: Discord via
[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter), as
JSON — the format `bin/dex normalize` reads. Other sources: convert to that
shape, or feed items through capture instead.

## Pulling from Discord (the interim manual model)

The owner asks in plain words — "run a discord update" — and a session
performs the pull. This is the manual model until watchers make feeding
sources a first-class engine layer; do not build instance machinery
around it. The per-instance facts — which server, which channels and
ids, that a token exists — live in the instance's own CLAUDE.md, and the
first move is to read them there (existing exports also carry
`channel.id` in their JSON).

- **Tooling.** DiscordChatExporter runs via Docker: check `docker info`,
  and `docker pull tyrrrz/discordchatexporter:stable` on first use. (DCE
  also ships CLI binaries; Docker is the documented path here.)
- **Auth.** The token is `DISCORD_TOKEN` in the instance's `.env`. A
  user token is passed bare — no `Bot ` prefix. Never echo it into
  output, a commit, or a report. To probe validity without exporting:
  `GET https://discord.com/api/v10/users/@me` with the token as the
  `Authorization` header.
- **Discovering ids** when the instance records none:
  `docker run --rm tyrrrz/discordchatexporter:stable guilds -t "$DISCORD_TOKEN"`,
  then `… channels -t "$DISCORD_TOKEN" -g <guild-id>`. Record what you
  learn in the instance's CLAUDE.md so the next session skips this step.
- **The export**, per channel, from the instance root (the mount must
  land under `raw/`):

  ```
  docker run --rm -v "$PWD/raw/discord/<channel>:/out" \
    tyrrrz/discordchatexporter:stable export -t "$DISCORD_TOKEN" \
    -c <channel-id> -f Json -o /out/messages.json --media --reuse-media
  ```

  Each run is a full re-export — safe, because normalize is idempotent
  and item ids are stable, so unchanged clusters rewrite nothing.
  `--media --reuse-media` downloads attachments beside the JSON and
  skips files already present.

Then the standard flow below takes over — normalize onward. Nothing
else about a Discord pull is special.

A converted export must give every message `id`, `type` (`Default` or
`Reply`), `timestamp` as ISO 8601, and an `author` carrying `id` plus
`name` or `nickname`; any attachment needs `url` and `fileName`. A message
missing one is skipped rather than fatal, and a whole channel whose file
cannot be read is named and skipped while the rest normalize — so read the
whole normalize summary before treating a backfill as complete: the
`warn:` lines, any `export unreadable — skipped` channel, and any
`channel incomplete` line. A silently short cohort is the failure this
reports.

`bin/dex normalize` → scope-filter pass (judgment; purge via `bin/dex
exclude <file.json>`) → `bin/dex enrich run` → the per-item work at scale.
Per-driver politeness sleeps make large cohorts slow by design, and the
pacing is automatic: fresh work drains first, then rerun cohorts
(migration reseeds and other requeues) at most 50 per run — the report
states the cohort position, and the remainder drains itself across
subsequent runs. No `--limit` babysitting needed. For 500+ items: work in
waves; write work manifests BEFORE dispatching any parallel agents; every
agent contract includes "if an input is missing, STOP — do not improvise";
verify coverage mechanically between waves.
