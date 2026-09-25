# Backfills (exports in raw/)

Getting exports: Discord via
[DiscordChatExporter](https://github.com/Tyrrrz/DiscordChatExporter), as
JSON — the format `bin/dex normalize` reads. Other sources: convert to that
shape, or feed items through capture instead.

## Pulling from Discord (the interim manual model)

The owner asks in plain words ("run a discord update") and the session
performs the pull. This is the manual model until watchers make feeding
sources a first-class engine layer; do not build instance machinery
around it.

The per-instance facts live in the `discord` key of `state/config.json`:
`guild` is the server's id, and `channels` maps the name of each channel
to pull to its id (`state-formats.md` has the shape). The first move of
every pull is to read that key. When it is absent, set it up with the
owner before anything else (**Setting up**, below). The token never goes
in config: it is `DISCORD_TOKEN` in the instance's `.env`.

- **Tooling.** DiscordChatExporter runs via Docker: check `docker info`,
  and `docker pull tyrrrz/discordchatexporter:stable` on first use. (DCE
  also ships CLI binaries; Docker is the documented path here.)
- **Auth.** The token is `DISCORD_TOKEN` in the instance's `.env`. A
  user token is passed bare — no `Bot ` prefix. Never echo it into
  output, a commit, or a report. To probe validity without exporting:
  `GET https://discord.com/api/v10/users/@me` with the token as the
  `Authorization` header.
- **Setting up**, when `state/config.json` has no `discord` key. Work
  through it with the owner, who asked for this pull in this session:
  1. Check that `DISCORD_TOKEN` is in `.env` and that the probe above
     accepts it. If it is missing or refused, ask the owner to put a
     working token there, and go no further until the probe accepts one.
  2. List the servers the token can see with
     `docker run --rm tyrrrz/discordchatexporter:stable guilds -t "$DISCORD_TOKEN"`,
     and ask the owner which one to pull.
  3. List that server's channels with
     `docker run --rm tyrrrz/discordchatexporter:stable channels -t "$DISCORD_TOKEN" -g <guild-id>`,
     and ask the owner which of them to pull.
  4. Add the `discord` key to `state/config.json`, leaving its other
     keys as they are: the chosen server's id as `guild`, and each chosen
     channel's name and id in `channels`.

  When `raw/discord/` already holds exports, each directory there is a
  channel pulled before, and its `messages.json` carries the server's id
  as `guild.id` and the channel's as `channel.id`. Offer that server and
  those channels as the answers to steps 2 and 3, each channel keyed by
  its directory name.
- **Channel names stay fixed.** A channel's name in `channels` is the
  `raw/discord/<name>/` directory its export lands in, and normalize
  derives item ids from that directory. Once a channel has been pulled,
  keep its name as it is, even after the channel is renamed in Discord:
  under a new name, every conversation in the channel is filed again as
  a new item. When the owner asks to add a channel, find its id with the
  listing in step 3 and add it to `channels`.
- **The export**, for each entry in `channels`, from the instance root
  (the mount must land under `raw/`), with `<channel>` the entry's name
  and `<channel-id>` its id:

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
