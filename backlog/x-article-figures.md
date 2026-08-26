# X article figures — the media the article fix does not fetch

The x driver now renders a long-form article's `content.blocks[]` as the
enrichment body, but an article's figures live elsewhere in the same
payload and are not read: `atomic` blocks carry `entityRanges` into
`content.entityMap` MEDIA entries, and `article.media_entities[]
.media_info.original_img_url` holds the image URLs. A session recovering
an article by hand downloaded and read those figures; the driver's version
of the same article has the prose and skips the images.

The mechanics are already in place: the driver pools post media into
`Content.media` and the media stage fetches under its existing caps
(4 files per item, 10MB per file). The work is mapping `media_entities`
into that same pool — and deciding whether an `atomic` block should leave
a positional marker in the body so a figure can be read in context rather
than as a loose file.

Recorded from the fourth field occurrence of the article defect (#61),
whose reporter recovered the figures by hand.
