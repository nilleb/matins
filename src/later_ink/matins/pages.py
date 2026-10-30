"""Server-rendered settings page.

Kept in the same no-JS style as the upstream pages: a form, a POST, a
confirmation. The CSS is inline because the app's CSP permits inline styles but
not inline script, and a settings page has nothing that needs script.
"""

from html import escape

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
       line-height: 1.5; max-width: 46em; margin: 2em auto; padding: 0 1em; }
h1 { font-size: 1.5em; margin-bottom: 0.2em; }
h2 { font-size: 1.1em; margin-top: 2em; }
p.small, .meta { font-size: 0.85em; opacity: 0.75; }
label { display: block; margin-top: 1em; font-weight: 600; font-size: 0.9em; }
input[type=text], input[type=email], input[type=url], input[type=time], textarea {
  width: 100%; box-sizing: border-box; padding: 0.5em; font: inherit;
  border: 1px solid #8888; border-radius: 4px; background: transparent; color: inherit; }
textarea { min-height: 9em; font-family: ui-monospace, monospace; font-size: 0.9em; }
button { margin-top: 1.2em; padding: 0.6em 1.2em; font: inherit; font-weight: 600;
         border: 1px solid #8888; border-radius: 4px; background: #8881; cursor: pointer; }
code { font-family: ui-monospace, monospace; font-size: 0.9em; }
ul { padding-left: 1.2em; }
.ok { color: #1a7f37; }
.err { color: #b42318; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def settings_page(
    *,
    secret: str,
    csrf: str,
    feeds: list[str],
    feed_lines: str,
    email: str,
    webhook_url: str,
    schedule: str,
    timezone: str,
    enabled: bool,
    issues: list[tuple[str, str, int, int]],
    webhook_secret_set: bool,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    banner = ""
    if notice:
        banner = f'<p class="ok">{escape(notice)}</p>'
    if error:
        banner = f'<p class="err">{escape(error)}</p>'

    issue_rows = "".join(
        f'<li><a href="/{escape(secret)}/issues/{escape(issue_id)}.epub">{escape(title)}</a> '
        f'<span class="meta">· {count} articles · {words:,} words</span></li>'
        for issue_id, title, count, words in issues
    ) or '<li class="meta">No issues yet. One is composed on your schedule, or now if you press the button.</li>'

    secret_note = (
        '<span class="meta">(a signing secret is set — leave blank to keep it)</span>'
        if webhook_secret_set
        else '<span class="meta">(optional; the receiver uses it to verify the signature)</span>'
    )

    body = f"""
<h1>Matins</h1>
<p class="small">Your catalogue: <code>/{escape(secret)}/</code> — add that URL (with
your server's address in front) to KOReader or any OPDS reader.</p>
{banner}

<h2>Sources</h2>
<form method="post" action="/{escape(secret)}/settings">
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <label for="feeds">RSS / Atom feeds — one URL per line</label>
  <textarea id="feeds" name="feeds" spellcheck="false"
    placeholder="https://example.com/feed.xml">{escape(feed_lines)}</textarea>
  <p class="meta">{len(feeds)} subscribed. Removing a line unsubscribes it;
  articles already printed in an issue are unaffected.</p>

  <h2>Delivery</h2>
  <label for="schedule">Compose an issue at</label>
  <input type="time" id="schedule" name="schedule" value="{escape(schedule)}">
  <label for="timezone">Timezone (IANA name)</label>
  <input type="text" id="timezone" name="timezone" value="{escape(timezone)}"
    placeholder="Europe/Paris">
  <label for="email">Notification email {'' if email else ''}</label>
  <input type="email" id="email" name="email" value="{escape(email)}"
    placeholder="you@example.com">
  <p class="meta">A subject line and a link. The issue itself is never attached.</p>

  <label for="webhook_url">Webhook URL</label>
  <input type="url" id="webhook_url" name="webhook_url" value="{escape(webhook_url)}"
    placeholder="https://example.com/hooks/matins">
  <label for="webhook_secret">Webhook signing secret</label>
  <input type="text" id="webhook_secret" name="webhook_secret" value=""
    placeholder="{escape('unchanged' if webhook_secret_set else 'whsec-like value')}">
  {secret_note}
  <p class="meta">POSTed on <code>issue.ready</code>, signed
  <code>t=&lt;unix&gt;,v1=&lt;hmac-sha256&gt;</code> over
  <code>timestamp.body</code> in the <code>X-Matins-Signature</code> header.</p>

  <label><input type="checkbox" name="enabled" value="1"
    {'checked' if enabled else ''}> Compose and deliver on schedule</label>

  <button type="submit">Save</button>
</form>

<h2>Compose now</h2>
<form method="post" action="/{escape(secret)}/issues/build">
  <input type="hidden" name="csrf" value="{escape(csrf)}">
  <p class="small">Builds an issue from what is new since the last one, then
  delivers it by webhook and email as configured.</p>
  <button type="submit">Compose an issue now</button>
</form>

<h2>Issues</h2>
<ul>{issue_rows}</ul>
"""
    return _page("Matins settings", body)


def notice_page(title: str, message: str, back_href: str) -> str:
    return _page(
        title,
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
        f'<p><a href="{escape(back_href)}">Back to settings</a></p>',
    )
