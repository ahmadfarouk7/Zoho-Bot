"""
Zoho Mail -> Telegram Meeting Bot
Run from E:\\  →  cd e:\\  &&  python zoho_meeting_bot.py

Requirements: pip install requests schedule
"""

import hashlib
import imaplib
import email
import re
import shutil
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr
import time
import schedule
import logging
import requests
import json
import os
import sys
from datetime import datetime, timezone, timedelta

DATA_DIR = os.environ.get("BOT_DATA_DIR", "e:\\")
os.makedirs(DATA_DIR, exist_ok=True)
_CAIRO_TZ = timezone(timedelta(hours=2), "EET")

CONFIG = {
    "zoho_email":    os.environ.get("ZOHO_EMAIL",    "####################"),
    "zoho_password": os.environ.get("ZOHO_PASSWORD", "####################"),
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "####################"),
    "telegram_chat_id":   os.environ.get("TELEGRAM_CHAT_ID",  "####################"),
    "check_interval_minutes": 5,
    "display_timezone": "Africa/Cairo",
    "reminder_minutes_before": [15, 60],
    "keywords": [
        "meeting", "invite", "invitation", "agenda", "zoom", "teams",
        "conference", "sync", "calendar", "webinar", "standup", "stand-up",
    ],
    "unseen_only": True,
    "first_run_lookback_days": 7,
    "history_keep_days": 30,
    "past_lookback_days": 14,
    "seen_ids_file":             os.path.join(DATA_DIR, "seen_email_ids.json"),
    "notified_meetings_file":    os.path.join(DATA_DIR, "notified_meetings.json"),
    "telegram_offset_file":      os.path.join(DATA_DIR, "telegram_offset.json"),
    "meetings_file":             os.path.join(DATA_DIR, "scheduled_meetings.json"),
    "meeting_history_file":      os.path.join(DATA_DIR, "meeting_history.json"),
    "log_file":                  os.path.join(DATA_DIR, "meeting_bot.log"),
}

ZOHO_IMAP_SERVER = "imappro.zoho.com"
ZOHO_IMAP_PORT   = 993
TELEGRAM_MAX_LEN = 4096

JOIN_LINK_RE = re.compile(
    r"https?://[^\s<>\"']+(?:zoom\.us|teams\.microsoft\.com|meet\.google\.com|"
    r"webex\.com|gotomeeting\.com|bluejeans\.com|whereby\.com)[^\s<>\"']*",
    re.IGNORECASE,
)
NOISE_LINE_RE = re.compile(
    r"(unsubscribe|view in browser|privacy policy|confidential|do not reply|"
    r"sent from my |copyright|all rights reserved|microsoft teams meeting)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def migrate_legacy_data():
    legacy = r"e:\plus"
    for name in (
        "seen_email_ids.json", "notified_meetings.json", "telegram_offset.json",
        "scheduled_meetings.json", "meeting_history.json",
    ):
        old_p = os.path.join(legacy, name)
        new_p = os.path.join(DATA_DIR, name)
        if os.path.exists(old_p) and not os.path.exists(new_p):
            shutil.copy2(old_p, new_p)
            log.info("Migrated %s -> %s", old_p, new_p)


def load_json_set(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_json_set(path, values):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(values), f, indent=2)


def load_telegram_offset():
    path = CONFIG["telegram_offset_file"]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("offset", 0))
    return 0


def save_telegram_offset(offset):
    with open(CONFIG["telegram_offset_file"], "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def load_meetings():
    path = CONFIG["meetings_file"]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_meetings(meetings):
    with open(CONFIG["meetings_file"], "w", encoding="utf-8") as f:
        json.dump(meetings, f, indent=2, ensure_ascii=False)


def load_meeting_history():
    path = CONFIG["meeting_history_file"]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_meeting_history(history):
    with open(CONFIG["meeting_history_file"], "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def normalize_meeting_title(subject):
    title = subject.strip()
    for pattern in (
        r"^updated invitation:\s*", r"^invitation:\s*", r"^reminder:\s*",
        r"^accepted:\s*", r"^declined:\s*", r"^canceled:\s*", r"^cancelled:\s*", r"^re:\s*",
    ):
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)
    return title.strip()


def meeting_id(subject, start_iso, ics_uid=""):
    if ics_uid:
        return hashlib.md5(ics_uid.strip().encode()).hexdigest()[:16]
    norm = normalize_meeting_title(subject).lower()
    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        rounded = start_dt.replace(minute=(start_dt.minute // 5) * 5, second=0, microsecond=0)
        key = f"{norm}|{rounded.astimezone(timezone.utc).isoformat()}"
    except Exception:
        key = f"{norm}|{start_iso}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def load_notified_meetings():
    return load_json_set(CONFIG["notified_meetings_file"])


def mark_meeting_notified(mid):
    notified = load_notified_meetings()
    notified.add(mid)
    save_json_set(CONFIG["notified_meetings_file"], notified)


def add_to_history(record):
    history = load_meeting_history()
    mid = record["id"]
    item = dict(record)
    item["saved_at"] = datetime.now(timezone.utc).isoformat()
    for i, h in enumerate(history):
        if h.get("id") == mid:
            merged = dict(h)
            for k, v in item.items():
                if v or not merged.get(k):
                    merged[k] = v
            history[i] = merged
            break
    else:
        history.append(item)
    cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG["history_keep_days"])
    kept = []
    for h in history:
        try:
            start = datetime.fromisoformat(h["start"]).replace(tzinfo=timezone.utc)
            if start >= cutoff:
                kept.append(h)
        except Exception:
            kept.append(h)
    save_meeting_history(kept)


def dedupe_meetings(records):
    best = {}
    for record in records:
        mid = record["id"]
        if mid not in best:
            best[mid] = record
            continue
        cur = best[mid]
        score = len(record.get("summary") or []) + (1 if record.get("link") else 0)
        cur_score = len(cur.get("summary") or []) + (1 if cur.get("link") else 0)
        if score >= cur_score:
            best[mid] = record
    return sorted(best.values(), key=lambda r: r["start"])


def upsert_meeting(record):
    meetings = load_meetings()
    mid = record["id"]
    for i, existing in enumerate(meetings):
        if existing["id"] == mid:
            sent = existing.get("reminders_sent", [])
            record["reminders_sent"] = list(set(sent + record.get("reminders_sent", [])))
            meetings[i] = record
            save_meetings(meetings)
            return
    meetings.append(record)
    save_meetings(meetings)


def prune_past_meetings():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    kept = []
    for m in load_meetings():
        try:
            start = datetime.fromisoformat(m["start"]).replace(tzinfo=timezone.utc)
            if start >= cutoff:
                kept.append(m)
        except Exception:
            pass
    save_meetings(kept)


def local_tz():
    name = CONFIG.get("display_timezone", "Africa/Cairo")
    if name in ("Africa/Cairo", "Egypt", "EET"):
        return _CAIRO_TZ
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _CAIRO_TZ


def decode_str(value):
    if value is None:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def strip_html(html):
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>|</div>|</li>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        html = html.replace(entity, char)
    return html


def normalize_body_text(text):
    text = strip_html(text) if "<" in text and ">" in text else text
    return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def get_body(msg):
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower() and ctype not in ("text/calendar", "application/ics"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(charset, errors="replace")
                html = text if msg.get_content_type() == "text/html" else ""
                plain = text if msg.get_content_type() != "text/html" else ""
        except Exception:
            pass
    if plain.strip():
        return normalize_body_text(plain)
    if html.strip():
        return normalize_body_text(html)
    return ""


def clean_text(text):
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def truncate_telegram(text, max_len=TELEGRAM_MAX_LEN):
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def extract_sender_name(sender):
    name, addr = parseaddr(sender)
    return name.strip() or addr.strip() or sender


def extract_join_link(text):
    match = JOIN_LINK_RE.search(text or "")
    return match.group(0).rstrip(".,;)") if match else ""


def format_dt_local(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).strftime("%a %d %b · %H:%M")


def format_dt_range(start, end=None):
    line = format_dt_local(start)
    if end:
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        line += f" – {end.astimezone(local_tz()).strftime('%H:%M')}"
    return f"{line} ({start.astimezone(local_tz()).strftime('%Z')})"


def human_time_until(target):
    now = datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = target - now
    if delta.total_seconds() <= 0:
        return "started"
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"in {mins} min"
    hours, rem = divmod(mins, 60)
    if hours < 24:
        return f"in {hours}h {rem}m" if rem else f"in {hours}h"
    return f"in {hours // 24}d {hours % 24}h"


def unfold_ics(text):
    lines = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return "\n".join(lines)


def parse_ics_datetime(value):
    value = value.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1]
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def extract_ics_events(msg):
    events = []
    for part in msg.walk():
        ctype = part.get_content_type()
        filename = part.get_filename() or ""
        if ctype not in ("text/calendar", "application/ics") and not filename.lower().endswith(".ics"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            ics_text = unfold_ics(payload.decode("utf-8", errors="replace"))
        except Exception:
            continue
        for block in ics_text.split("BEGIN:VEVENT"):
            if "END:VEVENT" not in block:
                continue
            summary = description = location = organizer = uid = ""
            dtstart = dtend = None
            for line in block.splitlines():
                if line.startswith("SUMMARY"):
                    summary = line.split(":", 1)[-1].strip()
                elif line.startswith("DESCRIPTION"):
                    description = line.split(":", 1)[-1].strip()
                elif line.startswith("DTSTART"):
                    dtstart = parse_ics_datetime(line.split(":", 1)[-1])
                elif line.startswith("DTEND"):
                    dtend = parse_ics_datetime(line.split(":", 1)[-1])
                elif line.startswith("LOCATION"):
                    location = line.split(":", 1)[-1].strip()
                elif line.startswith("UID"):
                    uid = line.split(":", 1)[-1].strip()
                elif line.startswith("ORGANIZER"):
                    organizer = re.sub(r"^mailto:", "", line.split(":", 1)[-1].strip(), flags=re.I)
            if dtstart:
                events.append({
                    "summary": summary,
                    "description": description.replace("\\n", "\n").replace("\\,", ","),
                    "start": dtstart, "end": dtend, "location": location,
                    "organizer": organizer, "uid": uid,
                })
    return events


def extract_meeting_time_from_text(text):
    patterns = [
        r"(?:when|date|time|scheduled|starts?)[:\s]+([^\n]{8,80})",
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4}[\s,]+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)",
        r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\s+\d{1,2},?\s+\d{4}[\s,]+\d{1,2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%m/%d/%Y %I:%M %p",
                    "%m/%d/%Y %H:%M", "%a, %b %d, %Y %H:%M", "%a %b %d %Y %H:%M"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def meaningful_lines(body):
    lines = []
    for raw in body.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if len(line) < 12 or NOISE_LINE_RE.search(line):
            continue
        if re.match(r"^[-_=]{5,}$", line):
            continue
        lines.append(line)
    return lines


def summarize_email_content(subject, body, ics_event=None):
    bullets = []
    if ics_event and ics_event.get("description"):
        for line in meaningful_lines(ics_event["description"])[:3]:
            bullets.append(line[:160])
    for pattern in (
        r"(?:agenda|description|topic|purpose|objective|notes|about)[:\s]+(.+)",
        r"(?:please join|join (?:us|the meeting)|meeting link)[:\s]+(.+)",
    ):
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            chunk = match.group(1).split("\n")[0].strip()
            if len(chunk) > 20:
                bullets.append(chunk[:160])
    if not bullets:
        for line in meaningful_lines(body):
            if subject.lower() not in line.lower()[:30]:
                bullets.append(line[:160])
            if len(bullets) >= 3:
                break
    if not bullets and body.strip():
        bullets.append(re.sub(r"\s+", " ", body).strip()[:200])
    seen, unique = set(), []
    for b in bullets:
        if b.lower() not in seen:
            seen.add(b.lower())
            unique.append(b)
    return unique[:4]


def build_meeting_record(msg, subject, sender, body, email_date):
    events = extract_ics_events(msg)
    ics = events[0] if events else None
    if ics:
        start, end = ics["start"], ics.get("end")
        title = normalize_meeting_title(ics.get("summary") or subject)
        location, organizer = ics.get("location", ""), ics.get("organizer") or extract_sender_name(sender)
        ics_uid = ics.get("uid", "")
    else:
        start = extract_meeting_time_from_text(body) or email_date
        end, location, ics_uid = None, "", ""
        title = normalize_meeting_title(subject)
        organizer = extract_sender_name(sender)
        loc_match = re.search(r"(?:location|where|venue)[:\s]+([^\n]{3,80})", body, re.I)
        if loc_match:
            location = loc_match.group(1).strip()
    link = extract_join_link(body) or (extract_join_link(ics.get("description", "")) if ics else "")
    start_iso = start.astimezone(timezone.utc).isoformat()
    return {
        "id": meeting_id(title, start_iso, ics_uid),
        "subject": title,
        "sender": sender,
        "organizer": organizer,
        "start": start_iso,
        "end": end.astimezone(timezone.utc).isoformat() if end else None,
        "location": location,
        "link": link,
        "summary": summarize_email_content(title, body, ics),
        "reminders_sent": [],
    }


def is_meeting_email(subject, body, msg):
    combined = (subject + " " + body).lower()
    return any(kw.lower() in combined for kw in CONFIG["keywords"]) or bool(extract_ics_events(msg))


def divider():
    return "────────────────"


def format_meeting_card(record, mode="new"):
    start = datetime.fromisoformat(record["start"]).replace(tzinfo=timezone.utc)
    end = None
    if record.get("end"):
        end = datetime.fromisoformat(record["end"]).replace(tzinfo=timezone.utc)
    icons = {"new": "📬", "upcoming": "📅", "past": "✅", "reminder": "⏰", "week": "📋"}
    labels = {"new": "New Meeting", "upcoming": "Upcoming", "past": "Past Meeting",
              "reminder": "Reminder", "week": "Meeting"}
    lines = [
        f"{icons.get(mode, '📅')} <b>{labels.get(mode, 'Meeting')}</b>",
        divider(),
        f"📌 <b>{clean_text(record['subject'])}</b>",
        f"🕐 {clean_text(format_dt_range(start, end))}",
    ]
    until = human_time_until(start)
    if mode in ("upcoming", "reminder", "new") and until != "started":
        lines.append(f"⏳ <i>{clean_text(until)}</i>")
    if record.get("organizer"):
        lines.append(f"👤 {clean_text(record['organizer'])}")
    if record.get("location"):
        lines.append(f"📍 {clean_text(record['location'])}")
    if record.get("link"):
        lines.append(f"🔗 {clean_text(record['link'])}")
    bullets = record.get("summary") or []
    if bullets:
        lines.extend(["", "📝 <b>Summary</b>"] + [f"  • {clean_text(b)}" for b in bullets])
    return "\n".join(lines)


def format_reminder_card(record, minutes_before):
    return f"{format_meeting_card(record, mode='reminder')}\n\n🔔 <b>Starts in {minutes_before} minutes</b>"


def format_week_header(week_start, week_end, past_count, upcoming_count):
    return (
        f"📊 <b>This Week's Meetings</b>\n{divider()}\n"
        f"📆 {week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}\n"
        f"✅ Past: <b>{past_count}</b>   ·   📅 Upcoming: <b>{upcoming_count}</b>"
    )


def format_week_compact(record, is_past):
    start = datetime.fromisoformat(record["start"]).replace(tzinfo=timezone.utc)
    icon = "✅" if is_past else "📅"
    line = f"{icon} <b>{format_dt_local(start)}</b> — {clean_text(record['subject'])}"
    if record.get("link") and not is_past:
        line += f"\n    🔗 {clean_text(record['link'])}"
    return line


def send_telegram(text):
    url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/sendMessage"
    payload = {"chat_id": CONFIG["telegram_chat_id"], "text": truncate_telegram(text),
               "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if not r.ok:
            log.error("Telegram API error %s: %s", r.status_code, r.text)
            if r.status_code == 400:
                payload.pop("parse_mode")
                payload["text"] = truncate_telegram(re.sub(r"<[^>]+>", "", text))
                payload["disable_web_page_preview"] = True
                r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
        log.info("Telegram message sent.")
        return True
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)
        return False


def connect_imap():
    mail = imaplib.IMAP4_SSL(ZOHO_IMAP_SERVER, ZOHO_IMAP_PORT)
    mail.login(CONFIG["zoho_email"], CONFIG["zoho_password"])
    mail.select("INBOX")
    return mail


def fetch_message_bytes(mail, eid):
    for query in ("(BODY.PEEK[])", "(RFC822)"):
        try:
            status, msg_data = mail.fetch(eid, query)
            if status != "OK" or not msg_data:
                continue
            chunks = [p[1] for p in msg_data if isinstance(p, tuple) and len(p) > 1 and isinstance(p[1], bytes)]
            if chunks:
                return max(chunks, key=len)
        except imaplib.IMAP4.error as e:
            log.warning("Fetch %s failed for %s: %s", query, eid, e)
    return None


def search_email_ids(mail, unseen_only, since=None):
    criteria = []
    if unseen_only:
        criteria.append("UNSEEN")
    if since:
        criteria.append(f'SINCE {since.strftime("%d-%b-%Y")}')
    query = " ".join(criteria) if criteria else "ALL"
    status, data = mail.search(None, query)
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def parse_email_message(raw):
    msg = email.message_from_bytes(raw)
    subject = decode_str(msg.get("Subject", "(No Subject)"))
    sender = decode_str(msg.get("From", "Unknown"))
    body = get_body(msg)
    try:
        email_date = parsedate_to_datetime(msg.get("Date", ""))
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=timezone.utc)
        else:
            email_date = email_date.astimezone(timezone.utc)
    except Exception:
        email_date = datetime.now(timezone.utc)
    record = build_meeting_record(msg, subject, sender, body, email_date)
    return msg, subject, sender, body, email_date, record


def get_week_range():
    today = datetime.now(timezone.utc)
    start = today - timedelta(days=today.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0), start + timedelta(days=6, hours=23, minutes=59, seconds=59)


def register_meeting_if_upcoming(record):
    start = datetime.fromisoformat(record["start"]).replace(tzinfo=timezone.utc)
    if start > datetime.now(timezone.utc):
        upsert_meeting(record)


def process_meeting_record(record, notify=False):
    add_to_history(record)
    register_meeting_if_upcoming(record)
    if not notify:
        return False
    notified = load_notified_meetings()
    if record["id"] in notified:
        log.info("Skipping duplicate meeting notification: %s", record["subject"])
        return False
    if send_telegram(format_meeting_card(record, mode="new")):
        mark_meeting_notified(record["id"])
        return True
    return False


def scan_meetings_since(since, until=None):
    mail = connect_imap()
    email_ids = search_email_ids(mail, unseen_only=False, since=since)
    records = []
    for eid in email_ids:
        raw = fetch_message_bytes(mail, eid)
        if not raw:
            continue
        msg, subject, sender, body, email_date, record = parse_email_message(raw)
        if not is_meeting_email(subject, body, msg):
            continue
        start = datetime.fromisoformat(record["start"]).replace(tzinfo=timezone.utc)
        if until and (start < since or start > until):
            continue
        records.append(record)
        add_to_history(record)
    mail.logout()
    return dedupe_meetings(records)


def check_reminders():
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=CONFIG["check_interval_minutes"])
    meetings, changed = load_meetings(), False
    for m in meetings:
        try:
            start = datetime.fromisoformat(m["start"]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if start <= now:
            continue
        sent = set(m.get("reminders_sent", []))
        for mins in CONFIG["reminder_minutes_before"]:
            if mins in sent:
                continue
            remind_at = start - timedelta(minutes=mins)
            if remind_at <= now < remind_at + window:
                if send_telegram(format_reminder_card(m, mins)):
                    sent.add(mins)
                    m["reminders_sent"] = sorted(sent)
                    changed = True
    if changed:
        save_meetings(meetings)
    prune_past_meetings()


def ensure_telegram_polling():
    url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/deleteWebhook"
    try:
        requests.get(url, params={"drop_pending_updates": False}, timeout=10).raise_for_status()
        log.info("Telegram webhook cleared.")
    except Exception as e:
        log.warning("Could not clear webhook: %s", e)


def handle_telegram_command(command):
    if command == "/week":
        send_telegram("⏳ <b>Fetching this week's meetings...</b>")
        get_this_week_meetings()
    elif command == "/today":
        send_telegram("⏳ <b>Fetching today's meetings...</b>")
        get_today_meetings()
    elif command == "/past":
        send_telegram("⏳ <b>Loading past meetings...</b>")
        get_past_meetings()
    elif command == "/status":
        upcoming = sum(1 for m in load_meetings()
                       if datetime.fromisoformat(m["start"]).replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        send_telegram(
            f"⚙️ <b>Bot Status</b>\n{divider()}\n"
            f"📁 Data: <code>{DATA_DIR}</code>\n"
            f"🔔 Reminders: <b>{', '.join(str(m) for m in CONFIG['reminder_minutes_before'])}</b> min\n"
            f"📅 Upcoming tracked: <b>{upcoming}</b>\n"
            f"📚 History: <b>{len(load_meeting_history())}</b> meetings"
        )
    elif command in ("/help", "/start"):
        send_telegram(
            f"🤖 <b>Commands</b>\n{divider()}\n"
            "/week — this week\n/today — today only\n/past — last 14 days\n/status — bot info\n/help"
        )


def poll_telegram_commands():
    offset = load_telegram_offset()
    url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 1, "allowed_updates": ["message"]}, timeout=10)
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as e:
        log.error("getUpdates failed: %s", e)
        return
    for update in updates:
        offset = update.get("update_id", 0) + 1
        message = update.get("message") or {}
        if str((message.get("chat") or {}).get("id", "")) != str(CONFIG["telegram_chat_id"]):
            continue
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        command = text.split()[0].lower().split("@")[0]
        log.info("Command: %s", command)
        try:
            handle_telegram_command(command)
        except Exception as e:
            log.error("Command %s failed: %s", command, e, exc_info=True)
            send_telegram(f"❌ <b>Error running {command}</b>\n{clean_text(str(e))}")
    if offset > load_telegram_offset():
        save_telegram_offset(offset)


def get_today_meetings():
    tz = local_tz()
    now_local = datetime.now(timezone.utc).astimezone(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    day_end = (now_local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) - timedelta(seconds=1)).astimezone(timezone.utc)
    try:
        records = scan_meetings_since(day_start - timedelta(days=14), until=day_end)
        today = [r for r in records if day_start <= datetime.fromisoformat(r["start"]).replace(tzinfo=timezone.utc) <= day_end]
        if not today:
            send_telegram(f"📭 No meetings for <b>{now_local.strftime('%A %d %b')}</b>.")
            return
        send_telegram(f"📅 <b>Today — {now_local.strftime('%A %d %b')}</b>\n{divider()}\n<b>{len(today)}</b> meeting(s)")
        now = datetime.now(timezone.utc)
        for record in today:
            start = datetime.fromisoformat(record["start"]).replace(tzinfo=timezone.utc)
            send_telegram(format_meeting_card(record, mode="past" if start < now else "upcoming"))
            time.sleep(0.4)
    except Exception as e:
        log.error("get_today_meetings: %s", e, exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{clean_text(str(e))}")


def get_past_meetings():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=CONFIG["past_lookback_days"])
    try:
        records = scan_meetings_since(since, until=now)
        past = [r for r in records if datetime.fromisoformat(r["start"]).replace(tzinfo=timezone.utc) < now]
        if not past:
            history_past = [h for h in load_meeting_history()
                            if datetime.fromisoformat(h["start"]).replace(tzinfo=timezone.utc) < now]
            past = dedupe_meetings(history_past)
        if not past:
            send_telegram(f"📭 No past meetings in the last <b>{CONFIG['past_lookback_days']}</b> days.")
            return
        send_telegram(
            f"✅ <b>Past Meetings</b> (last {CONFIG['past_lookback_days']} days)\n"
            f"{divider()}\n<b>{len(past)}</b> meeting(s)"
        )
        for record in past:
            send_telegram(format_meeting_card(record, mode="past"))
            time.sleep(0.4)
    except Exception as e:
        log.error("get_past_meetings: %s", e, exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{clean_text(str(e))}")


def get_this_week_meetings():
    week_start, week_end = get_week_range()
    now = datetime.now(timezone.utc)
    try:
        records = scan_meetings_since(week_start, until=week_end)
        past = [r for r in records if datetime.fromisoformat(r["start"]).replace(tzinfo=timezone.utc) < now]
        upcoming = [r for r in records if datetime.fromisoformat(r["start"]).replace(tzinfo=timezone.utc) >= now]
        if not past and not upcoming:
            send_telegram("📭 No meetings found this week.")
            return
        send_telegram(format_week_header(week_start, week_end, len(past), len(upcoming)))
        if upcoming:
            send_telegram("📅 <b>Upcoming</b>\n\n" + "\n\n".join(format_week_compact(r, False) for r in upcoming))
            time.sleep(0.4)
            for record in upcoming:
                register_meeting_if_upcoming(record)
                send_telegram(format_meeting_card(record, mode="week"))
                time.sleep(0.4)
        if past:
            send_telegram("✅ <b>Past</b>\n\n" + "\n\n".join(format_week_compact(r, True) for r in past))
            time.sleep(0.4)
            for record in past:
                send_telegram(format_meeting_card(record, mode="past"))
                time.sleep(0.4)
    except imaplib.IMAP4.error as e:
        log.error("IMAP error: %s", e)
        send_telegram(f"❌ <b>IMAP error</b>\n{clean_text(str(e))}")
    except Exception as e:
        log.error("get_this_week_meetings: %s", e, exc_info=True)
        send_telegram(f"❌ <b>Error</b>\n{clean_text(str(e))}")


def check_email():
    log.info("Checking Zoho Mail...")
    seen_ids = load_json_set(CONFIG["seen_ids_file"])
    first_run = len(seen_ids) == 0
    try:
        mail = connect_imap()
        since = None
        if first_run:
            since = datetime.now(timezone.utc) - timedelta(days=CONFIG["first_run_lookback_days"])
        email_ids = search_email_ids(mail, unseen_only=CONFIG["unseen_only"] and not first_run, since=since)
        if not email_ids:
            mail.logout()
            return
        new_count = 0
        for eid in email_ids:
            eid_str = eid.decode() if isinstance(eid, bytes) else str(eid)
            if eid_str in seen_ids:
                continue
            raw = fetch_message_bytes(mail, eid)
            seen_ids.add(eid_str)
            if not raw:
                continue
            msg, subject, sender, body, email_date, record = parse_email_message(raw)
            if is_meeting_email(subject, body, msg):
                log.info("Meeting email: %s", subject)
                if process_meeting_record(record, notify=True):
                    new_count += 1
        save_json_set(CONFIG["seen_ids_file"], seen_ids)
        mail.logout()
        log.info("Done. %d new meeting notification(s).", new_count)
    except Exception as e:
        log.error("check_email: %s", e, exc_info=True)


def refresh_upcoming_meetings():
    try:
        since = datetime.now(timezone.utc) - timedelta(days=14)
        for record in scan_meetings_since(since):
            register_meeting_if_upcoming(record)
    except Exception as e:
        log.error("refresh_upcoming_meetings: %s", e)


if __name__ == "__main__":
    migrate_legacy_data()
    if len(sys.argv) > 1 and sys.argv[1] == "week":
        get_this_week_meetings()
    else:
        log.info("Bot started. Data dir: %s", DATA_DIR)
        send_telegram(
            f"🤖 <b>Meeting Bot running</b>\n{divider()}\n"
            f"📁 <code>{DATA_DIR}</code>\n"
            f"🔔 Reminders: <b>{', '.join(str(m) for m in CONFIG['reminder_minutes_before'])}</b> min before\n\n"
            "/week · /today · /past · /status · /help"
        )
        ensure_telegram_polling()
        refresh_upcoming_meetings()
        check_email()
        check_reminders()
        poll_telegram_commands()
        schedule.every(CONFIG["check_interval_minutes"]).minutes.do(check_email)
        schedule.every(CONFIG["check_interval_minutes"]).minutes.do(check_reminders)
        schedule.every(6).hours.do(refresh_upcoming_meetings)
        schedule.every().monday.at("08:00").do(get_this_week_meetings)
        while True:
            schedule.run_pending()
            poll_telegram_commands()
            time.sleep(12)
