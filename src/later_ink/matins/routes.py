"""Matins HTTP surface: settings, save-to-queue, issues over OPDS, admin run.

Mounted before the tenant routes in `later_ink.main`, which is what makes
`/{secret}/issues/` reach these handlers instead of the generic folder route.

Auth is the catalogue secret itself, the same bearer-in-the-URL model the rest of
the app uses. The settings form additionally carries a CSRF token derived from
the app key, because it is the one page that changes state from a browser.
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import config as app_config
from .. import opds
from ..connectors.base import Article
from . import catalogues, config, pages
from .issues import IssueService, local_day

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_FEEDS = 100
MAX_URL_LENGTH = 2048
EPUB_MEDIA = "application/epub+zip"


# --------------------------------------------------------------- validation


def _valid_url(raw: str) -> str | None:
    value = raw.strip()
    if not value or len(value) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return value


# ------------------------------------------------------------ main helpers
# Imported lazily: `later_ink.main` imports this module while it is still
# executing, so a module-level import would be circular.


def _resolve(secret: str, request: Request) -> str:
    from ..main import _resolve_secret

    return _resolve_secret(secret, request)


def _csrf_token(secret: str) -> str:
    from ..main import _csrf_token as token

    return token(secret)


def _require_csrf(secret: str, token: str | None) -> None:
    from ..main import _require_csrf as require

    require(secret, token)


def _origin(request: Request) -> str:
    from ..main import _feed_origin

    return _feed_origin(request)


def _store(request: Request):
    return request.app.state.matins


def _service(request: Request) -> IssueService:
    return request.app.state.issues


# ----------------------------------------------------------------- settings


@router.get("/{secret}/settings", response_class=HTMLResponse)
async def settings_get(secret: str, request: Request, saved: int = 0):
    _resolve(secret, request)
    return HTMLResponse(_render_settings(request, secret, notice="Settings saved." if saved else None))


@router.post("/{secret}/settings")
async def settings_post(
    secret: str,
    request: Request,
    csrf: str = Form(""),
    feeds: str = Form(""),
    email: str = Form(""),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    schedule: str = Form(""),
    timezone: str = Form(""),
    enabled: str = Form(""),
):
    _resolve(secret, request)
    _require_csrf(secret, csrf)

    requested: list[str] = []
    invalid: list[str] = []
    for line in feeds.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url = _valid_url(line)
        if url is None:
            invalid.append(line)
        elif url not in requested:
            requested.append(url)

    if len(requested) > MAX_FEEDS:
        return HTMLResponse(
            _render_settings(
                request,
                secret,
                error=f"That is {len(requested)} feeds; the limit is {MAX_FEEDS}.",
                feeds_override="\n".join(requested),
            ),
            status_code=400,
        )
    if invalid:
        shown = ", ".join(invalid[:3])
        more = "" if len(invalid) <= 3 else f" (and {len(invalid) - 3} more)"
        return HTMLResponse(
            _render_settings(
                request,
                secret,
                error=f"Not a usable http(s) URL: {shown}{more}",
                feeds_override="\n".join(requested),
            ),
            status_code=400,
        )

    store = _store(request)
    existing = {feed.url: feed for feed in store.list_feeds(secret)}
    for url in requested:
        if url not in existing:
            store.add_feed(secret, url)
    for url, feed in existing.items():
        if url not in requested:
            store.delete_feed(secret, feed.id)

    schedule = _normalize_schedule(schedule) or config.get_default_schedule()
    timezone = (timezone or "").strip() or config.get_default_timezone()
    if not _valid_timezone(timezone):
        return HTMLResponse(
            _render_settings(
                request,
                secret,
                error=f"Unknown timezone: {timezone}",
                feeds_override="\n".join(requested),
            ),
            status_code=400,
        )

    previous = store.get_delivery(secret)
    kept_secret = _secrets_unchanged(previous, webhook_secret)
    store.set_delivery(
        secret,
        webhook_url=webhook_url,
        webhook_secret=kept_secret,
        email=email,
        schedule=schedule,
        timezone=timezone,
        enabled=bool(enabled),
    )
    return RedirectResponse(f"/{secret}/settings?saved=1", status_code=303)


@router.get("/{secret}/issues/preview", response_class=HTMLResponse)
async def issues_preview(secret: str, request: Request):
    """The next issue's selection, without writing anything."""
    _resolve(secret, request)
    reader = await _reader(request, secret)
    candidates = await _service(request).plan(secret, reader)
    return HTMLResponse(
        pages.preview_page(
            secret=secret,
            csrf=_csrf_token(secret),
            candidates=[
                {
                    "title": c.article.title,
                    "section": c.section,
                    "source": c.source,
                    "words": c.estimated_words,
                }
                for c in candidates
            ],
            window_hours=config.get_issue_window_hours(),
            max_articles=config.get_issue_max_articles(),
        )
    )


@router.post("/{secret}/issues/build", response_class=HTMLResponse)
async def build_now(secret: str, request: Request, csrf: str = Form("")):
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    delivery = _store(request).get_delivery(secret)
    day = local_day(delivery.timezone if delivery else config.get_default_timezone())
    issue = await _build(request, secret, day)
    if issue is None:
        message = "Nothing new to compose — no unprinted feed items and an empty queue."
    else:
        message = (
            f"Composed “{issue.title}” with {issue.article_count} articles. "
            "It is in your Issues folder now."
        )
        await _service(request).deliver(secret, issue)
    return HTMLResponse(
        pages.notice_page("Matins", message, f"/{secret}/settings")
    )


# ------------------------------------------------------------ save-to-queue


@router.post("/{secret}/forget")
async def forget_catalogue(secret: str, request: Request, csrf: str = Form("")):
    """Drop this catalogue from the browser's list. Not a deletion."""
    _resolve(secret, request)
    _require_csrf(secret, csrf)
    response = RedirectResponse("/", status_code=303)
    catalogues.forget(secret, request, response)
    return response


@router.post("/{secret}/save")
async def save(secret: str, request: Request):
    _resolve(secret, request)
    url = ""
    title = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected a JSON object") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        url = str(body.get("url") or "")
        title = body.get("title")
        title = str(title)[:300] if title else None
    else:
        form = await request.form()
        url = str(form.get("url") or "")
        title = form.get("title")
        title = str(title)[:300] if title else None

    url = _valid_url(url)
    if not url:
        raise HTTPException(400, "Expected an http(s) url")
    queued = _store(request).save_url(secret, url, title)
    return JSONResponse({"queued": queued, "url": url}, status_code=201 if queued else 200)


# --------------------------------------------------------------- issues OPDS


@router.api_route("/{secret}/issues/", methods=["GET", "HEAD"])
async def issues_feed(secret: str, request: Request):
    _resolve(secret, request)
    issues = _service(request).issues_for(secret)
    articles = [
        Article(
            id=issue.id,
            title=issue.title,
            summary=_issue_summary(issue),
            updated=datetime.fromtimestamp(issue.created_at, tz=UTC).replace(tzinfo=None),
            word_count=issue.word_count,
        )
        for issue in issues
    ]
    return _feed_response(
        request, secret, articles, title="Issues", feed_id="issues", epub_base="issues"
    )


@router.get("/{secret}/issues/{issue_id}.epub")
async def issue_epub(secret: str, issue_id: str, request: Request):
    _resolve(secret, request)
    try:
        path = _service(request).issue_path(secret, issue_id)
    except KeyError:
        raise HTTPException(404, "No such issue") from None
    try:
        data = path.read_bytes()
    except OSError:
        # The row exists but the file is gone — a volume that was replaced, or a
        # prune that raced a download. A 410 says "this edition is gone" rather
        # than "your catalogue is broken".
        raise HTTPException(410, "That issue is no longer on disk") from None
    issue = _store(request).get_issue(secret, issue_id)
    filename = f"{(issue.title if issue else 'matins').replace('—', '-')}.epub"
    filename = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in filename)
    return Response(
        data,
        media_type=EPUB_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------- admin


@router.post("/admin/issues/run")
async def admin_run(request: Request, secret: str | None = None, force: int = 0):
    token = config.get_admin_token()
    if not token:
        raise HTTPException(404)  # no admin surface unless explicitly enabled
    provided = request.headers.get("x-admin-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(provided.encode(), token.encode()):
        raise HTTPException(403, "Bad admin token")

    store = _store(request)
    targets = [secret] if secret else [d.secret for d in store.list_deliveries()]
    built: list[str] = []
    for target in targets:
        delivery = store.get_delivery(target)
        day = local_day(delivery.timezone if delivery else config.get_default_timezone())
        if not force and store.has_issue_for_day(target, day):
            continue
        issue = await _build(request, target, day)
        if issue is None:
            continue
        built.append(issue.id)
        await _service(request).deliver(target, issue)
    return {"built": built}


# -------------------------------------------------------------------- shared


async def _reader(request: Request, secret: str):
    try:
        return await request.app.state.tenant_reader(secret)
    except KeyError:
        raise HTTPException(404, "Unknown catalogue") from None


async def _build(request: Request, secret: str, day: str):
    return await _service(request).build(
        secret=secret, reader=await _reader(request, secret), local_day=day
    )


def dashboard(request: Request, secret: str) -> str:
    """HTML for `/{secret}/` when a browser asks for it. Called from main's
    tenant root, which owns the route and the cookie."""
    store = _store(request)
    issues = [
        (i.id, i.title, i.article_count, i.word_count)
        for i in _service(request).issues_for(secret)[:5]
    ]
    return pages.dashboard_page(
        secret=secret,
        base_url=app_config.get_base_url(),
        csrf=_csrf_token(secret),
        feed_count=len(store.list_feeds(secret)),
        saved_count=len(store.list_saved(secret)),
        issues=issues,
        latest=issues[0] if issues else None,
    )


def _feed_response(request, secret, articles, *, title, feed_id, epub_base):
    return Response(
        opds.article_feed(
            f"urn:later-ink:u:{_hash(secret)}:{feed_id}",
            title,
            articles,
            self_href=f"/{secret}/{epub_base}/",
            epub_base=f"/{secret}/{epub_base}",
            start_href=f"/{secret}/",
            origin=_origin(request),
        ),
        media_type=opds.ACQ_TYPE,
    )


def _hash(secret: str) -> str:
    return hashlib.sha1(secret.encode()).hexdigest()[:12]


def _issue_summary(issue) -> str:
    minutes = max(1, round(issue.word_count / 250))
    return f"{issue.article_count} articles · about {minutes} min"


def _render_settings(request, secret, *, notice=None, error=None, feeds_override=None):
    store = _store(request)
    delivery = store.get_delivery(secret)
    feeds = [f.url for f in store.list_feeds(secret)]
    issues = [
        (i.id, i.title, i.article_count, i.word_count)
        for i in _service(request).issues_for(secret)[:20]
    ]
    return pages.settings_page(
        secret=secret,
        csrf=_csrf_token(secret),
        feeds=feeds,
        feed_lines=feeds_override if feeds_override is not None else "\n".join(feeds),
        # `or ""` rather than a truthiness check: a saved-but-blank field is
        # stored as NULL, and escape(None) is a 500 on the page that saves it.
        email=(delivery.email if delivery else None) or "",
        webhook_url=(delivery.webhook_url if delivery else None) or "",
        schedule=delivery.schedule if delivery else config.get_default_schedule(),
        timezone=delivery.timezone if delivery else config.get_default_timezone(),
        enabled=bool(delivery.enabled) if delivery else True,
        issues=issues,
        webhook_secret_set=bool(delivery and delivery.webhook_secret),
        notice=notice,
        error=error,
    )


def _secrets_unchanged(previous, submitted: str) -> str | None:
    """Resolve the webhook secret field.

    Blank keeps the existing one, so the form never has to echo a signing key
    back into the page (where it would sit in browser history). A lone "-"
    clears it.
    """
    submitted = submitted.strip()
    if submitted == "-":
        return None
    if submitted:
        return submitted
    return previous.webhook_secret if previous else None


def _normalize_schedule(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    hour, _, minute = value.partition(":")
    try:
        h, m = int(hour), int(minute or 0)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return f"{h:02d}:{m:02d}"


def _valid_timezone(name: str) -> bool:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True
