# Paid media/object storage

S3 or Cloudflare R2 as a media store
replacing/augmenting LFS (LFS free tier = 1GB), and/or as capture staging.
Storage is one substitutable ingest step — capture clients and pointers are
unaffected. Worth deciding only once an image-heavy instance shows real
volume; note the access-control trade: LFS keeps media behind the repo's
own permissions, a public bucket does not.
