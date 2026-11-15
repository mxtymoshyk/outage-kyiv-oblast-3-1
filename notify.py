#!/usr/bin/env python3
"""DTEK Kyiv-oblast outage schedule -> Telegram + calendar feed.

Messages:
  * schedule  - when today's/tomorrow's schedule for the group changes or a new day
                is published: DTEK day-table picture + text (news, then each day)
  * reminder  - ~REMIND_MIN minutes before each planned outage
  * health    - once if the data source is broken for 1h+, once when it recovers
Messages between QUIET hours are silent. Also writes outage.ics. Stdlib only
(Pillow is installed lazily just for the fallback picture).

Data: github.com/Baskerville42/outage-data-ua. JSON and pictures are read from one
commit, so the picture always matches the text.

Env:
  TG_TOKEN, TG_CHAT_ID  bot token / comma-separated chat ids (empty = dry run)
  GROUP        default GPV3.1          REGION      default kyiv-region
  REMIND_MIN   default 60 (0 = off)    QUIET       default "23-7" (empty = never)
  STALE_MIN    default 180             STATE_FILE  default state.json
  ICS_FILE     default outage.ics
  tests: SOURCE_FILE (local JSON), TEST_NOW (ISO datetime), NO_IMAGE, FORCE
"""
import datetime as dt
import hashlib
import html
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
GROUP = os.environ.get("GROUP", "GPV3.1")
REGION = os.environ.get("REGION", "kyiv-region")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
ICS_FILE = os.environ.get("ICS_FILE", "outage.ics")
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60") or 0)
QUIET = os.environ.get("QUIET", "23-7")
STALE_MIN = int(os.environ.get("STALE_MIN", "180"))   # source older than this = broken
ALERT_AFTER_MIN = 60                                   # problem must persist this long
ICS_KEEP_DAYS = 14

MIRROR = "Baskerville42/outage-data-ua"
DAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
Q = GROUP.replace("GPV", "")
IMG_NAMES = [f"gpv-{Q.replace('.', '-')}-emergency.png",   # day table + legend only
             f"gpv-{Q.replace('.', '-')}.png"]             # fallback: day + weekly table

# hour-slot value -> (first half, second half)
SLOT = {
    "yes": ("on", "on"), "no": ("off", "off"),
    "first": ("off", "on"), "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"), "msecond": ("on", "maybe"),
}


# ---------- time & schedule helpers ----------
def now_kyiv():
    t = os.environ.get("TEST_NOW")
    return dt.datetime.fromisoformat(t).replace(tzinfo=KYIV) if t else dt.datetime.now(KYIV)


def half_hours(slots):
    """DTEK hour slots {"1".."24": value} -> 48 half-hour states."""
    out = []
    for h in range(1, 25):
        out.extend(SLOT.get(slots.get(str(h), "yes"), ("on", "on")))
    return out


def hm(i, start=False):
    """Half-hour index -> HH:MM. Index 48 is 24:00 as an end, 00:00 (next day) as a start."""
    if i > 48 or (start and i >= 48):
        i -= 48
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def dur(n_half):
    h, half = divmod(n_half, 2)
    if not h:
        return "30 хв"
    return f"{h},5 год" if half else f"{h} год"


def until(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h} год {m} хв" if h and m else f"{h} год" if h else f"{m} хв"


def runs(states):
    """[(state, start, end)] for consecutive equal states."""
    res, i = [], 0
    while i < len(states):
        j = i
        while j < len(states) and states[j] == states[i]:
            j += 1
        res.append((states[i], i, j))
        i = j
    return res


def off_list(states):
    return [[a, b] for s, a, b in runs(states) if s == "off"]


def total_off(offs):
    return sum(b - a for a, b in offs)


def span_list(idx):
    """Half-hour indices -> 'HH:MM–HH:MM, …'"""
    out, idx = [], sorted(idx)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and idx[j + 1] == idx[j] + 1:
            j += 1
        out.append(f"{hm(idx[i])}–{hm(idx[j] + 1)}")
        i = j + 1
    return ", ".join(out)


def ranges(offs):
    return ", ".join(f"{hm(a)}–{hm(b)}" for a, b in offs)


def schedule_hash(states):
    return hashlib.sha1("".join(states).encode()).hexdigest()[:12]


# ---------- data source ----------
def mirror_commit():
    """Latest commit of the mirror (git protocol: no API rate limits). None = use 'main'."""
    try:
        out = subprocess.run(["git", "ls-remote", f"https://github.com/{MIRROR}", "refs/heads/main"],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        sha = out.split()[0]
        return sha if len(sha) == 40 else None
    except Exception as e:
        print(f"::warning::mirror commit not resolved, using main: {e}")
        return None


def raw_url(ref, path):
    return f"https://raw.githubusercontent.com/{MIRROR}/{ref or 'main'}/{path}"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "outage-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def load_source(ref):
    if os.environ.get("SOURCE_FILE"):
        with open(os.environ["SOURCE_FILE"], encoding="utf-8") as f:
            return json.load(f)
    return json.loads(http_get(raw_url(ref, f"data/{REGION}.json")))


def fetch_mirror_image(ref, name):
    try:
        data = http_get(raw_url(ref, f"images/{REGION}/{name}"))
        return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except Exception as e:
        print(f"::warning::image {name} not loaded: {e}")
        return None


def group_days(src, today):
    """{date: 48 states} for our group, today and later."""
    days = {}
    for ts, groups in ((src.get("fact") or {}).get("data") or {}).items():
        d = dt.datetime.fromtimestamp(int(ts), KYIV).date()
        if d >= today and GROUP in groups:
            days[d] = half_hours(groups[GROUP])
    return days


def source_problem(src, days, now):
    """None if the data is usable, else a short human reason."""
    if src is None:
        return "не вдалося завантажити дані"
    try:
        upd = dt.datetime.fromisoformat(src["lastUpdated"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return "невідомий формат даних"
    age = (now - upd).total_seconds() / 60
    if age > STALE_MIN:
        return f"дані не оновлювались {until(age)}"
    st = src.get("lastUpdateStatus") or {}
    if st.get("ok") is False:
        return f"сайт ДТЕК не віддає графік (код {st.get('code')})"
    if not days:
        return f"у даних немає графіка для черги {Q}"
    return None


# ---------- message text ----------
def day_label(date, today):
    if date == today:
        return "Сьогодні"
    if date == today + dt.timedelta(days=1):
        return "Завтра"
    return f"{date:%d.%m}"


def day_block(date, states, today, prev_off=None, is_new=False):
    """One self-contained block per day: header · total, times, and what changed."""
    offs = off_list(states)
    total = total_off(offs)
    edited = prev_off is not None and prev_off != offs
    mark = " ✏️" if edited else " 🆕" if is_new else ""
    head = f"📅 <b>{day_label(date, today)}, {DAYS_UA[date.weekday()]} {date:%d.%m}</b>{mark}"
    lines = [f"{head} · без світла {dur(total)}" if total else f"{head} · без відключень 🟢"]
    if offs:
        lines.append(f"🔴 {ranges(offs)}")
    maybe = [f"{hm(a)}–{hm(b)}" for x, a, b in runs(states) if x == "maybe"]
    if maybe:
        lines.append(f"🟡 Можливо: {', '.join(maybe)}")
    if edited:
        old = {i for a, b in prev_off for i in range(a, b)}
        new = {i for a, b in offs for i in range(a, b)}
        if new - old:
            lines.append(f"<i>↳ тепер без світла: {span_list(new - old)}</i>")
        if old - new:
            lines.append(f"<i>↳ тепер зі світлом: {span_list(old - new)}</i>")
    return "\n".join(lines)


def news_line(days, today, changed, fresh, sent):
    """Top line(s) = the news; it is what a lock-screen preview shows. None = no news."""
    if not changed:
        return None

    def word(d):
        return day_label(d, today).lower() if d <= today + dt.timedelta(days=1) else f"{d:%d.%m}"

    news = []
    for d in sorted(changed):
        offs = off_list(days[d])
        prev = sent.get(d.isoformat(), {}).get("off")
        if d in fresh:
            amount = f"без світла {dur(total_off(offs))}" if offs else "без відключень"
            news.append(f"🆕 <b>Графік на {word(d)}</b> · {amount}")
        elif prev != offs:
            net = total_off(offs) - total_off(prev)
            delta = (f"без світла на {dur(abs(net))} {'більше' if net > 0 else 'менше'}"
                     if net else "час без світла той самий")
            news.append(f"✏️ <b>Змінився графік на {word(d)}</b> · {delta}")
    return "\n".join(news) or "🟡 <b>Оновлено можливі відключення</b>"


def schedule_text(days, today, changed, sent, update_stamp):
    first = not sent
    fresh = [d for d in changed if d.isoformat() not in sent]
    news = None if first else news_line(days, today, changed, fresh, sent)
    parts = [news] if news else []
    for d in sorted(days):                                   # chronological
        prev = None if first else sent.get(d.isoformat(), {}).get("off")
        parts.append(day_block(d, days[d], today, prev, is_new=(d in fresh and not first)))
    parts.append(f"<i>Черга {Q} · ДТЕК, оновлено {html.escape(str(update_stamp))}</i>")
    return "\n\n".join(parts)


def reminders_due(t_today, t_tom, now, reminded):
    """[(key, text)] for outages starting within REMIND_MIN that weren't reminded yet."""
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    line = t_today + (t_tom or ["on"] * 48)
    mins_now = now.hour * 60 + now.minute
    out = []
    for s, a, b in runs(line):
        if s != "off" or a == 0:          # a == 0: continues from yesterday, not a new outage
            continue
        lead = a * 30 - mins_now
        key = f"{(today if a < 48 else tomorrow).isoformat()}@{hm(a, True)}"
        if 0 < lead <= REMIND_MIN and key not in reminded:
            end = f"{hm(b)}{' завтра' if b > 48 else ''}"
            out.append((key, f"⏰ <b>О {hm(a, True)} вимкнуть світло</b> · через {until(lead)}\n"
                             f"До {end} · {dur(b - a)} без світла"))
    return out


# ---------- own picture (fallback when the mirror has no day-table image) ----------
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def import_pillow():
    """Pillow is only needed for this rare fallback, so it's installed lazily in CI."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        if not os.environ.get("GITHUB_ACTIONS"):
            return None
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--user",
                        "--break-system-packages", "pillow"], check=True, timeout=180)
        import importlib, site
        sys.path.append(site.getusersitepackages())
        importlib.invalidate_caches()
        from PIL import Image, ImageDraw, ImageFont
    return Image, ImageDraw, ImageFont


def render_image(days, today, update_stamp):
    """Mobile-first day bars drawn from the data itself: no dependency on mirror pictures."""
    pil = import_pillow()
    if not pil:
        return None
    Image, ImageDraw, ImageFont = pil
    font = lambda size, bold=False: ImageFont.truetype(
        os.path.join(FONT_DIR, f"IBMPlexSans-{'SemiBold' if bold else 'Regular'}.ttf"), size)
    INK, MUTED, POWER, OUT, MAYBE, PILL = "#111827", "#6B7280", "#E5E7EB", "#111827", "#9CA3AF", "#FDE68A"
    W, PAD, BAR_H, scale = 900, 40, 56, 2          # drawn at 2x for crisp text
    row_h = 40 + BAR_H + 34 + 28
    H = 130 + row_h * len(days) + 50
    im = Image.new("RGB", (W * scale, H * scale), "white")
    d = ImageDraw.Draw(im)
    S = lambda *v: [x * scale for x in v]

    d.text(S(PAD, 34), "Графік відключень", fill=INK, font=font(34 * scale, True))
    pill = f"Черга {Q}"
    pw = d.textlength(pill, font=font(26 * scale, True)) / scale + 32
    d.rounded_rectangle(S(W - PAD - pw, 34, W - PAD, 78), radius=22 * scale, fill=PILL)
    d.text(S(W - PAD - pw + 16, 39), pill, fill=INK, font=font(26 * scale, True))
    d.text(S(PAD, 86), f"ДТЕК · оновлено {update_stamp}", fill=MUTED, font=font(20 * scale))

    x0, x1 = PAD, W - PAD
    seg = (x1 - x0) / 48
    y = 136
    for date in sorted(days):
        states = days[date]
        offs = off_list(states)
        d.text(S(PAD, y), f"{day_label(date, today)}, {DAYS_UA[date.weekday()]} {date:%d.%m}",
               fill=INK, font=font(26 * scale, True))
        right = f"без світла {dur(total_off(offs))}" if offs else "без відключень"
        rf = font(22 * scale)
        d.text((x1 * scale - d.textlength(right, font=rf), (y + 4) * scale), right, fill=MUTED, font=rf)
        by = y + 42
        bar = Image.new("RGB", (int((x1 - x0) * scale), BAR_H * scale), POWER)
        bd = ImageDraw.Draw(bar)
        for st, a, b in runs(states):
            if st != "on":
                bd.rectangle([a * seg * scale, 0, b * seg * scale, BAR_H * scale], fill=OUT if st == "off" else MAYBE)
        for hh in range(1, 24):                     # hour separators, stronger every 6h
            x = hh * 2 * seg * scale
            bd.line([x, 0, x, BAR_H * scale], fill="white", width=(3 if hh % 6 == 0 else 1) * scale)
        mask = Image.new("L", bar.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bar.size[0] - 1, bar.size[1] - 1], radius=12 * scale, fill=255)
        im.paste(bar, (x0 * scale, by * scale), mask)
        tf = font(18 * scale)
        for hh in range(0, 25, 3):
            lbl = f"{hh:02d}"
            tx = (x0 + hh * 2 * seg) * scale - d.textlength(lbl, font=tf) / 2
            d.text((tx, (by + BAR_H + 6) * scale), lbl, fill=MUTED, font=tf)
        y += row_h

    lx, ly = PAD, y + 4
    for color, label in ((POWER, "Світло є"), (OUT, "Світла немає"), (MAYBE, "Можливо")):
        d.rounded_rectangle(S(lx, ly + 3, lx + 22, ly + 25), radius=5 * scale, fill=color)
        d.text(S(lx + 32, ly), label, fill=MUTED, font=font(20 * scale))
        lx += 32 + d.textlength(label, font=font(20 * scale)) / scale + 36
    im = im.resize((W, H), Image.LANCZOS)
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def picture(ref, days, today, update_stamp):
    """Mirror's day table (same commit) -> own drawing -> mirror's full table -> no picture."""
    if os.environ.get("NO_IMAGE") or os.environ.get("SOURCE_FILE"):
        return None
    png = fetch_mirror_image(ref, IMG_NAMES[0])
    if png:
        return png
    try:
        png = render_image(days, today, update_stamp)
        if png:
            print("::notice::mirror day image missing, sent own drawing")
            return png
    except Exception as e:
        print(f"::warning::own drawing failed: {e}")
    return fetch_mirror_image(ref, IMG_NAMES[1])


# ---------- telegram ----------
def is_quiet(now):
    if not QUIET:
        return False
    a, b = (int(x) for x in QUIET.split("-"))
    return (now.hour >= a or now.hour < b) if a > b else (a <= now.hour < b)


def tg(method, fields, file=None):
    url = f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/{method}"
    if file:  # multipart upload
        boundary = "outagebot" + hashlib.sha1(file).hexdigest()[:16]
        body = b"".join(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                        for k, v in fields.items())
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"schedule.png\"\r\n"
                 "Content-Type: image/png\r\n\r\n").encode() + file + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode(errors="replace")}
    except Exception as e:  # network errors
        return {"ok": False, "description": str(e)}


def send(text, now, photo=None):
    """Deliver to every chat; photo with the text as caption, text-only on photo errors.
    Returns True if at least one chat got it (a bad id must not cause resends to the rest)."""
    silent = is_quiet(now)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        tag = f" + photo {len(photo) // 1024} KB" if photo else ""
        print(f"---- DRY RUN{' (silent)' if silent else ''}{tag} ----\n{text}\n")
        return True
    common = {"parse_mode": "HTML", "disable_notification": "true" if silent else "false"}
    file_id, ok = None, 0
    for cid in [c.strip() for c in chat.split(",") if c.strip()]:
        body = {"ok": False}
        if photo:
            fields = {**common, "chat_id": cid, "caption": text}
            body = tg("sendPhoto", {**fields, "photo": file_id}) if file_id else tg("sendPhoto", fields, file=photo)
            if body.get("ok") and not file_id:
                file_id = body["result"]["photo"][-1]["file_id"]   # upload once, reuse for others
            if not body.get("ok"):
                print(f"::warning::photo to …{cid[-3:]} failed, sending text: {body.get('description')}")
        if not body.get("ok"):
            body = tg("sendMessage", {**common, "chat_id": cid, "text": text,
                                      "disable_web_page_preview": "true"})
        if body.get("ok"):
            ok += 1
        else:  # repo is public: never log full chat ids
            print(f"::warning::Chat …{cid[-3:]} not delivered: {body.get('description')}")
    return ok > 0


# ---------- calendar feed (.ics) ----------
def ics_events(cal):
    """{iso_date: states48} -> [(state, start_dt, end_dt)], glued across midnight."""
    evs = []
    for iso in sorted(cal):
        base = dt.datetime.combine(dt.date.fromisoformat(iso), dt.time(), KYIV)
        for st, a, b in runs(cal[iso]):
            if st == "on":
                continue
            start, end = base + dt.timedelta(minutes=30 * a), base + dt.timedelta(minutes=30 * b)
            if evs and evs[-1][0] == st and evs[-1][2] == start:
                evs[-1] = (st, evs[-1][1], end)
            else:
                evs.append((st, start, end))
    return evs


def ics_text(cal):
    utc = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//outage-bot//UA",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:Світло · черга {Q}", "X-WR-TIMEZONE:Europe/Kyiv",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M",
    ]
    for st, a, b in ics_events(cal):
        off = st == "off"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{utc(a)}-{utc(b)}-{st}-{GROUP}@outage-bot",
            f"DTSTAMP:{utc(a)}", f"DTSTART:{utc(a)}", f"DTEND:{utc(b)}",
            f"SUMMARY:{'🔴 Без світла' if off else '🟡 Можливе відключення'}",
            f"DESCRIPTION:Черга {Q} · графік ДТЕК",
            "TRANSP:OPAQUE" if off else "TRANSP:TRANSPARENT",
        ]
        if off:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Скоро вимкнуть світло",
                      "TRIGGER:-PT30M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------- files ----------
def write_atomic(path, text, newline=None):
    """Write via a temp file + rename, so a killed run never leaves a half-written file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    os.replace(tmp, path)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    write_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


# ---------- main ----------
def main():
    now = now_kyiv()
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    state = load_state()
    failed = []          # deliveries that didn't reach anyone (state not advanced -> retried)

    ref = None if os.environ.get("SOURCE_FILE") else mirror_commit()
    try:
        src = load_source(ref)
    except Exception as e:
        print(f"::warning::source load failed: {e}")
        src = None
    days = group_days(src, today) if src else {}

    # 0) health: alert once if broken for ALERT_AFTER_MIN+, once more when it recovers
    problem = source_problem(src, days, now)
    health = state.get("health", {})
    if problem:
        health.setdefault("since", now.isoformat(timespec="minutes"))
        age = (now - dt.datetime.fromisoformat(health["since"])).total_seconds() / 60
        if age >= ALERT_AFTER_MIN and not health.get("alerted"):
            if send(f"⚠️ <b>Бот не бачить актуального графіка</b>\n{html.escape(problem)}.\n"
                    f"Поки не відновиться, звіряйся з dtek-krem.com.ua\n<i>Черга {Q}</i>", now):
                health["alerted"] = True
            else:
                failed.append("health")
    elif health.get("alerted"):
        if send(f"✅ <b>Дані про графік знову надходять</b>\n<i>Черга {Q}</i>", now):
            health = {}
        else:
            failed.append("health")
    else:
        health = {}
    state["health"] = health

    if days:
        sent = state.get("days", {})                 # iso date -> {"hash", "off"}
        reminded = set(state.get("reminded", []))

        # 1) schedule message on change (manual "Run workflow" always sends)
        changed = [d for d, s in days.items() if sent.get(d.isoformat(), {}).get("hash") != schedule_hash(s)]
        # FORCE comes from the workflow input: manual "Run workflow" -> true, external
        # 15-min trigger -> false. Without FORCE, any manual dispatch forces (old workflow).
        if "FORCE" in os.environ:
            force = os.environ["FORCE"].lower() in ("1", "true")
        else:
            force = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        if changed or force:
            stamp = (src.get("fact") or {}).get("update", "—")
            text = schedule_text(days, today, changed, sent, stamp)
            if send(text, now, photo=picture(ref, days, today, stamp)):
                for d in changed:
                    sent[d.isoformat()] = {"hash": schedule_hash(days[d]), "off": off_list(days[d])}
            else:
                failed.append("schedule")

        # 2) reminders
        if REMIND_MIN:
            for key, text in reminders_due(days.get(today, ["on"] * 48), days.get(tomorrow), now, reminded):
                if send(text, now):
                    reminded.add(key)
                else:
                    failed.append("reminder")

        # 3) calendar: current days + last ICS_KEEP_DAYS of history
        enc = {"on": "o", "off": "x", "maybe": "?"}
        cal = state.get("cal", {})
        cal.update({d.isoformat(): "".join(enc[x] for x in s) for d, s in days.items()})
        cutoff = (today - dt.timedelta(days=ICS_KEEP_DAYS)).isoformat()
        cal = {k: v for k, v in cal.items() if k >= cutoff}
        dec = {v: k for k, v in enc.items()}
        body = ics_text({k: [dec[c] for c in v] for k, v in cal.items()})
        try:
            with open(ICS_FILE, encoding="utf-8", newline="") as f:
                unchanged = f.read() == body
        except FileNotFoundError:
            unchanged = False
        if not unchanged:
            write_atomic(ICS_FILE, body, newline="")

        keep = {d.isoformat() for d in days}
        state.update({
            "days": {k: v for k, v in sent.items() if k in keep},
            "reminded": sorted(k for k in reminded if k[:10] >= today.isoformat()),
            "cal": cal,
        })
        print("changed:", [d.isoformat() for d in changed])

    save_state(state)
    if failed:
        # state is saved first, so what did go out is not resent; the red run makes GitHub email you
        sys.exit(f"Telegram: not delivered: {', '.join(failed)}")


if __name__ == "__main__":
    main()
