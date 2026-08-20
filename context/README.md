# context/

**The declared facts this service reads are not in this repo.** They are
personal, this repo is public, and the two should not mix.

They live in the private `bdrolet/context` repo, whose CI concatenates its
per-domain files and publishes the result as a version of the `standing-context`
secret. `terraform/cloud_functions.tf` mounts that secret read-only at
`/etc/context/standing-context.md` and points `STANDING_CONTEXT_PATH` at it, so
`services/standing_context.py` reads a path either way and needs no branch for
the two cases.

Changing a fact is a PR in that repo. It takes effect on the next cold start
here — no deploy of this service, which is why `context/**` no longer needs to
be in `.github/workflows/deploy.yml`'s path filter.

## Local development

`services/standing_context.py` falls back to `context/standing-context.md` when
`STANDING_CONTEXT_PATH` is unset, so you can drop a scratch file here to exercise
the triage gate — this directory is gitignored apart from this README and the
example below. Do not commit real facts.

See `standing-context.example.md` for the format, and the private repo's README
for how to write a fact that expires correctly.

## If the facts cannot be read

`section()` returns `""` and the triage gate falls open: mail becomes tasks
again, which is noisy but never lossy. A broken mount degrades quality; it
cannot swallow mail.
