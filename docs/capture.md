# Capture protocol

How content gets into a dex instance from outside a session. The Send To Dex
shortcut (`docs/shortcut.md`) is one client of this protocol; any HTTP client —
a bookmarklet, a CLI alias, a share extension — can implement it.

## The contract

One capture = one markdown file in `inbox/` at the instance repo root, created
through the GitHub contents API. The commit is the delivery; there is no
server-side machinery. Ingest processes each file and deletes it.

```
PUT https://api.github.com/repos/{owner}/{repo}/contents/inbox/{name}.md
Authorization: Bearer <token>
Accept: application/vnd.github+json

{ "message": "capture", "content": "<base64 of the file, no line wrapping>" }
```

- `{name}` — a timestamp, `yyyyMMdd-HHmmss`. Unique per capturer per second,
  which is all the uniqueness a personal inbox needs.
- The token needs Contents read/write on the instance repo (the same
  fine-grained or classic PAT the shortcut uses).
- The note — why this was worth saving — goes **in the file body**, never the
  commit message.

## Text capture

The file body is the captured URL and/or the note, nothing more:

```
https://example.com/post

why I saved it
```

## Binary capture (image, PDF, any file)

The binary never enters git history. It stages as an asset on the instance's
standing `inbox` release (every instance has one; `bin/dex inbox ensure`
creates it), and the capture file points at it:

1. **Find the release:**
   `GET /repos/{owner}/{repo}/releases/tags/inbox` → take `id` from the response.
2. **Upload the binary raw** (no base64, one request):

   ```
   POST https://uploads.github.com/repos/{owner}/{repo}/releases/{id}/assets?name={name}.{ext}
   Authorization: Bearer <token>
   Content-Type: application/octet-stream

   <raw bytes>
   ```

   Take `url` from the response — the asset's API URL.
3. **PUT the capture file** as above, with this body:

   ```
   ---
   asset: <asset api url from step 2>
   name: {name}.{ext}
   ---

   the note
   ```

At the next ingest, `bin/dex inbox` downloads the asset into `media/<id>/`
(where LFS applies), rewrites the capture's frontmatter to `media:`, and
deletes the asset. End state: media in LFS, git history text-only, release
empty.

## curl reference implementation

```sh
repo=owner/instance token=ghp_... stamp=$(date +%Y%m%d-%H%M%S)

# text
printf '%s\n\n%s\n' "https://example.com/post" "why I saved it" | base64 | tr -d '\n' | \
  xargs -I{} curl -s -X PUT -H "Authorization: Bearer $token" \
    "https://api.github.com/repos/$repo/contents/inbox/$stamp.md" \
    -d "{\"message\":\"capture\",\"content\":\"{}\"}"

# binary
rel=$(curl -s -H "Authorization: Bearer $token" \
  "https://api.github.com/repos/$repo/releases/tags/inbox" | jq .id)
asset=$(curl -s -X POST -H "Authorization: Bearer $token" \
  -H "Content-Type: application/octet-stream" --data-binary @photo.jpg \
  "https://uploads.github.com/repos/$repo/releases/$rel/assets?name=$stamp.jpg" | jq -r .url)
printf -- '---\nasset: %s\nname: %s\n---\n\n%s\n' "$asset" "$stamp.jpg" "the note" | \
  base64 | tr -d '\n' | \
  xargs -I{} curl -s -X PUT -H "Authorization: Bearer $token" \
    "https://api.github.com/repos/$repo/contents/inbox/$stamp.md" \
    -d "{\"message\":\"capture\",\"content\":\"{}\"}"
```

## Failure modes

- **Silent 404**: a token without access to the repo gets a 404 with a
  valid-looking JSON body — naive clients read it as success. Verify the
  commit actually landed when testing a new client or repo.
- **Wrapped base64**: the contents API rejects base64 containing line breaks.
  Encode without wrapping.
- **Orphaned assets**: if the asset uploads but the pointer PUT fails, the
  asset sits on the release unreferenced. `bin/dex inbox` reports these at
  every run.
