# Matins: architecture and decisions

Matins is the Matins fork's service layer on top of Later.Ink. This note records
*why* the pieces look the way they do, so a future change does not have to
rediscover the constraints.

## The shape

```
feeds (RSS/Atom) ─┐
saved queue      ─┼─► TenantReader ─► IssueService ─► EPUB on disk
Readwise         ─┘   (one catalogue)   selection      │
                                                       ├─► OPDS  /<secret>/issues/
                                                       ├─► webhook  issue.ready
                                                       └─► email    (link only)
```

Everything user-facing hangs off the catalogue secret (`/<secret>/`), the same
unguessable-in-practice bearer URL Later.Ink already uses. There is no login,
because an e-reader cannot do one conveniently — the URL *is* the credential.

## Why a fork rather than a plugin

Later.Ink's connectors are one-account-per-connector, and a tenant maps to one
connector. Issues need the union of several sources per user, plus write access
to a queue, plus scheduled work. All three are outside the connector contract,
so the fork adds:

| Piece | File |
|---|---|
| Multi-source fan-out with id namespacing | `matins/tenant.py` |
| RSS/Atom parsing and fetching | `matins/rss.py` |
| Built-in queue | `matins/queue.py` |
| Article extraction | `matins/extract.py` |
| Issue composition | `matins/issue_epub.py`, `matins/issues.py` |
| Signed webhooks | `matins/webhooks.py` |
| Notification email | `matins/notify.py` |
| Scheduler (asyncio loop) | `matins/scheduler.py` |
| Settings, save, issues OPDS, admin | `matins/routes.py` |
| Tables (feeds, saved, issues, delivery) | `matins/db.py` |

`later_ink/main.py` changes in four places: the router include, the tenant
connector factory, the Issues entry in the catalogue, and the lifespan wiring.
Keeping the diff there small is what makes `git merge upstream/main` survivable.

## Decisions worth remembering

**No new runtime dependencies.** RSS, extraction, HMAC, scheduling and SMTP are
`lxml` + stdlib. Every added package is a supply-chain surface to pin, hash and
patch; upstream already goes to real lengths here (hashed lockfiles,
digest-pinned images, locked build backend, `pip-audit` in CI). Extraction is the
weak point of this trade — the heuristic in `extract.py` is a simplified
readability — but it degrades to the feed's own content rather than failing.

**Ids: prefixed for new sources, bare for Readwise.** `feeds:…` and `saved:…`
route a download back to their connector; Readwise keeps upstream's bare ids so
existing OPDS URLs and KOReader progress entries survive. Unprefixed ids resolve
to Readwise, which is also what an old client sends.

**Dedupe keys are catalogue ids, not connector ids.** An issue records
`issue_items` with the same string the download route uses. The first
implementation compared raw connector ids against stored prefixed ones and
reprinted the whole feed every day; the test
`test_compose_now_builds_a_downloadable_issue` covers the regression.

**Email is notification-only.** No attachment, no Send-to-Kindle path, no 25 MB
ceiling. The issue is pulled. This also means SMTP being down never costs a
reader their issue.

**Webhooks are SSRF-guarded.** The URL is user-supplied; the same address check
the image fetcher applies (`fetch._validate`, public IPs only) runs before every
attempt, and redirects are not followed. `WEBHOOK_ALLOW_PRIVATE=1` is the
self-hoster's explicit opt-out for LAN targets such as Home Assistant.

**An issue is written to disk, not rendered per request.** It is an edition:
fixed membership, fixed bytes. That is what lets two devices share reading
position through KOReader's content-hash matching.

**Catch-up, not a missed day.** The scheduler considers a job due any time after
its scheduled local time while today's issue is missing, so a restart at 09:00
still produces the 07:00 paper. Failed builds retry every 30 minutes rather than
every tick.

## Data

SQLite, same file as the users table (WAL), disjoint tables:

- `feeds (id, secret, url, title, added_at)` — unique per `(secret, url)`.
- `saved (id, secret, url, title, added_at, consumed_by)` — the queue; re-saving
  a consumed link re-queues it.
- `issues (id, secret, title, created_at, local_day, path, article_count,
  word_count)` — `local_day` is the user's calendar day, used to answer "already
  built today?".
- `issue_items (issue_id, secret, article_id, title, url, section)` — membership
  and the dedupe set.
- `delivery (secret, webhook_url, webhook_secret, email, schedule, timezone,
  enabled)` — the webhook secret is Fernet-encrypted with the app key.

Issue files: `ISSUES_DIR/<sha1(secret)[:16]>/<issue-id>.epub`, written to a temp
name then renamed. The download route looks the path up in the database and
never builds one from the request.

## Configuration

See `deploy/oci/env.example` for the annotated list. The ones that change
behaviour meaningfully: `ISSUE_WINDOW_HOURS` (how far back a feed item may be),
`ISSUE_MAX_ARTICLES` / `ISSUE_MAX_PER_FEED` / `ISSUE_MAX_WORDS`,
`ISSUE_RETENTION`, `ADMIN_TOKEN`, and the SMTP group.

## Known limits

- No full-text extraction from paywalled or JS-only pages; those fall back to
  the feed summary.
- A feed that dates nothing is always considered fresh (it would otherwise never
  be printed), so a static page can reappear if its link changes.
- Search across sources is capped, not paginated.
- Issue language is fixed to `en` until per-feed language is tracked.
- No metrics endpoint; `/health` reports configuration, logs report builds.
