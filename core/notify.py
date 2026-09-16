"""Email notifications.

Two things, both optional and both off unless SMTP is configured:

* **Instant alert** when someone submits a request, so you can reply the same
  hour. A researcher who has just asked for help is warm; a day later they have
  emailed three other consultancies.
* **Daily digest** at whatever time you schedule it, listing yesterday's
  requests and usage with dates.

Nothing here can break the app. Every send is wrapped, and a failed email is
logged and swallowed — a visitor's request must still be saved even if your
mail provider is down.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

HOST = os.environ.get("SMTP_HOST", "")
PORT = int(os.environ.get("SMTP_PORT", "587"))
USER = os.environ.get("SMTP_USER", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER = os.environ.get("SMTP_FROM", "") or USER
TO = [a.strip() for a in os.environ.get("NOTIFY_TO", "").split(",") if a.strip()]
COMPANY = os.environ.get("JF_COMPANY", "CS Software Solutions")
PUBLIC_URL = os.environ.get("JF_PUBLIC_URL", "")


def configured() -> bool:
    return bool(HOST and SENDER and TO)


def _send(subject: str, body_text: str, body_html: str = "") -> tuple[bool, str]:
    if not configured():
        return False, "SMTP not configured"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SENDER
    msg["To"] = ", ".join(TO)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    try:
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, timeout=20,
                                  context=ssl.create_default_context()) as s:
                if USER:
                    s.login(USER, PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(HOST, PORT, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                if USER:
                    s.login(USER, PASSWORD)
                s.send_message(msg)
        return True, "sent"
    except Exception as exc:                       # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
def alert_new_request(req: dict, service_title: str) -> tuple[bool, str]:
    """Fire-and-forget alert for a single new enquiry."""
    when = (req.get("at") or "")[:16].replace("T", " ")
    lines = [
        f"New request — {service_title}",
        f"Received: {when} UTC",
        "",
        f"Name:        {req.get('name', '')}",
        f"Email:       {req.get('email', '')}",
        f"Phone:       {req.get('phone') or '-'}",
        f"Institution: {req.get('institution') or '-'}",
        f"Country:     {req.get('country') or '-'}",
        f"Role:        {req.get('role') or '-'}",
        f"Field:       {req.get('domain') or '-'}",
        f"Journal:     {req.get('journal') or '-'}",
        f"Timeline:    {req.get('deadline') or '-'}",
        f"Came from:   {req.get('source') or '-'}",
        "",
        "Message:",
        req.get("message") or "-",
    ]
    if PUBLIC_URL:
        lines += ["", f"Admin: {PUBLIC_URL}/Admin"]
    subject = (f"[{COMPANY}] {service_title} — {req.get('name', 'someone')}"
               f" ({req.get('institution') or req.get('country') or '—'})")
    return _send(subject, "\n".join(lines))


def digest(days: int = 1) -> tuple[bool, str]:
    """Summary of the last `days` days. Run from cron; see digest.py."""
    from . import services as svc

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()

    rows = [r for r in svc.all_requests() if (r.get("at") or "") >= since_iso]
    with svc.db() as conn:
        runs = conn.execute(
            "SELECT at, journal FROM runs WHERE at >= ? ORDER BY at",
            (since_iso,)).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) c FROM requests WHERE status='New'").fetchone()["c"]

    label = "yesterday" if days == 1 else f"the last {days} days"
    head = (f"{len(rows)} request(s) and {len(runs)} document(s) formatted "
            f"in {label}.")

    lines = [f"{COMPANY} — activity for {label}", "=" * 52, "", head, ""]

    if rows:
        lines.append("REQUESTS")
        lines.append("-" * 52)
        for r in rows:
            title = svc.BY_ID.get(r["service"], {}).get("title", r["service"])
            when = (r.get("at") or "")[:16].replace("T", " ")
            lines += [
                f"{when} UTC · {title} · {r['status']}",
                f"    {r['name']} <{r['email']}>"
                f"{' · ' + r['phone'] if r.get('phone') else ''}",
                f"    {r.get('institution') or '-'} · {r.get('country') or '-'}",
                f"    Field: {r.get('domain') or '-'} · "
                f"Journal: {r.get('journal') or '-'} · "
                f"Timeline: {r.get('deadline') or '-'}",
                f"    {(r.get('message') or '')[:300]}",
                "",
            ]
    else:
        lines += ["No new requests.", ""]

    if runs:
        by_journal: dict[str, int] = {}
        for r in runs:
            by_journal[r["journal"]] = by_journal.get(r["journal"], 0) + 1
        lines += ["DOCUMENTS FORMATTED", "-" * 52]
        for j, n in sorted(by_journal.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>3}  {j}")
        lines.append("")

    lines += [f"Unactioned requests in total: {pending}"]
    if PUBLIC_URL:
        lines += ["", f"Admin: {PUBLIC_URL}/Admin"]

    subject = f"[{COMPANY}] {len(rows)} request(s), {len(runs)} formatted — {label}"
    return _send(subject, "\n".join(lines))


def test() -> tuple[bool, str]:
    return _send(f"[{COMPANY}] Test email",
                 "If you are reading this, notifications are configured "
                 "correctly.")
