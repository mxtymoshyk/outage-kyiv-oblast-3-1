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
commit, so the picture always matches the text. If the mirror is broken, the bot reads
DTEK's site itself with a headless browser (Playwright, installed only then).

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
import time
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
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "7"))  # morning "Графік на сьогодні" (first run after it)
STALE_MIN = int(os.environ.get("STALE_MIN", "180"))   # source older than this = broken
ALERT_AFTER_MIN = 60                                   # problem must persist this long
ICS_KEEP_DAYS = 14

MIRROR = "Baskerville42/outage-data-ua"
DTEK_URL = "https://www.dtek-krem.com.ua/ua/shutdowns"   # direct fallback when the mirror is broken
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
    return f"{h}½ год" if half else f"{h} год"      # "7½", not "7,5": read as a phrase, no decimal


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


STATE_CODE = {"on": "o", "off": "x", "maybe": "?"}


def encode_states(states):
    return "".join(STATE_CODE[x] for x in states)


def prev_states(entry):
    """Last announced 48 states of a day. Older state files kept only the outage list."""
    if entry.get("s"):
        back = {v: k for k, v in STATE_CODE.items()}
        return [back[c] for c in entry["s"]]
    out = ["on"] * 48
    for a, b in entry.get("off", []):
        out[a:b] = ["off"] * (b - a)
    return out


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


def extract_fact(page_html):
    """`DisconSchedule.fact = {...}` from the DTEK page -> dict (or None).
    The object is followed by `</script>` on the same line, so find its end by brace matching."""
    i = page_html.find("DisconSchedule.fact")
    start = page_html.find("{", i) if i >= 0 else -1
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(start, len(page_html)):
        c = page_html[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    fact = json.loads(page_html[start:k + 1])
                except ValueError:
                    return None
                return fact if isinstance(fact, dict) and fact.get("data") else None
    return None


def import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        pass
    pip = [sys.executable, "-m", "pip", "install", "-q", "--user", "--break-system-packages", "playwright"]
    subprocess.run(pip, check=True, timeout=240)
    import importlib, site
    sys.path.append(site.getusersitepackages())
    importlib.invalidate_caches()
    subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                   check=True, timeout=300)
    from playwright.sync_api import sync_playwright
    return sync_playwright


def scrape_dtek():
    """Read the schedule straight from DTEK's site with a real headless browser
    (the site's anti-bot check needs JS + cookies), same way the mirror does it.
    Only used in CI and only while the mirror is broken: it takes 1–2 min to set up."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return None
    try:
        sync_playwright = import_playwright()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
                locale="uk-UA", timezone_id="Europe/Kyiv")
            try:
                for attempt in range(1, 4):
                    page = ctx.new_page()
                    try:
                        page.goto(DTEK_URL, wait_until="networkidle", timeout=90_000)
                        page.wait_for_timeout(500)
                        fact = extract_fact(page.content())
                        if fact:
                            now = dt.datetime.now(dt.timezone.utc)
                            return {"lastUpdated": now.isoformat().replace("+00:00", "Z"),
                                    "lastUpdateStatus": {"ok": True, "code": 200},
                                    "fact": fact, "via": "dtek"}
                        print(f"::warning::DTEK page without schedule (anti-bot?), attempt {attempt}/3")
                    except Exception as e:
                        print(f"::warning::DTEK attempt {attempt}/3 failed: {e}")
                    finally:
                        page.close()
                    if attempt < 3:
                        time.sleep(2 * attempt)             # back off before retrying
            finally:
                browser.close()
    except Exception as e:
        print(f"::warning::direct DTEK scrape unavailable: {e}")
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


def day_message(kind, date, states, today, prev_off, update_stamp, direct=False):
    """One message = one day. kind: digest (morning) · new (day published) · change ·
    maybe (only possible outages changed) · show (manual run / first run).
    First two lines are what a lock-screen preview shows: which day, and how much without power."""
    icon = {"digest": "☀️", "new": "🆕", "change": "✏️", "maybe": "🟡", "show": "⚡"}[kind]
    offs = off_list(states)
    total = total_off(offs)
    label = day_label(date, today)
    when = (f"на {label.lower()}, " if label in ("Сьогодні", "Завтра") else "на ") + \
        f"{DAYS_UA[date.weekday()]} {date:%d.%m}"
    title = f"{icon} <b>{'Змінився графік' if kind == 'change' else 'Графік'} {when}</b>"
    summary = f"без світла {dur(total)}" if total else "без відключень 🟢"
    if kind == "change":
        net = total - total_off(prev_off)                 # say what happened to the outages
        summary += " · " + (f"відключення подовжили на {dur(net)}" if net > 0 else
                            f"відключення скоротили на {dur(-net)}" if net < 0 else "відключення перенесли")
    elif kind == "maybe":
        summary += " · змінились можливі відключення"

    notes, cancelled = change_notes(prev_off, offs) if kind == "change" else ({}, [])
    rows = []
    for a, b in offs:
        note = notes.get((a, b))
        rows.append((a, f"🔴 {hm(a)}–{hm(b)}" + (f" <i>· {note}</i>" if note else "")))
    for a, b in cancelled:
        rows.append((a, f"⚪ <s>{hm(a)}–{hm(b)}</s> <i>· скасовано</i>"))
    for x, a, b in runs(states):
        if x == "maybe":
            rows.append((a, f"🟡 {hm(a)}–{hm(b)} <i>· можливо</i>"))
    via = "ДТЕК (напряму з сайту)" if direct else "ДТЕК"
    parts = [f"{title}\n{summary}"]
    if rows:
        parts.append("\n".join(text for _, text in sorted(rows)))
    parts.append(f"<i>Черга {Q} · {via}, оновлено {html.escape(str(update_stamp))}</i>")
    return "\n\n".join(parts)


def change_notes(prev_off, offs):
    """Outage changes like a train timetable: each new/changed outage gets "було …" or
    "нове"; fully removed ones are returned to show as cancelled.
    Every time range in a message means an outage, so power windows are never shown as ranges."""
    old = [tuple(x) for x in prev_off if list(x) not in offs]
    new = [tuple(x) for x in offs if list(x) not in prev_off]
    overlap = lambda p, q: p[0] < q[1] and q[0] < p[1]
    fmt = lambda xs: ", ".join(f"{hm(a)}–{hm(b)}" for a, b in sorted(xs))
    notes, cancelled, used_new = {}, [], set()
    used_old = set()
    for i in range(len(old)):                        # group overlapping old/new outages
        if i in used_old:
            continue
        g_old, g_new, grew = {i}, set(), True
        while grew:
            grew = False
            for j, n in enumerate(new):
                if j not in g_new and any(overlap(old[k], n) for k in g_old):
                    g_new.add(j); grew = True
            for k, o in enumerate(old):
                if k not in g_old and any(overlap(o, new[j]) for j in g_new):
                    g_old.add(k); grew = True
        used_old |= g_old
        used_new |= g_new
        if g_new:
            for j in g_new:
                notes[new[j]] = f"було {fmt(old[k] for k in g_old)}"
        else:
            cancelled += [old[k] for k in g_old]
    for j, n in enumerate(new):
        if j not in used_new:
            notes[n] = "нове"
    return notes, sorted(cancelled)


def key_minutes(key):
    """'2026-11-16@22:30' -> minutes since that date's midnight, counted from 2000-01-01."""
    day, t = key.split("@")
    d = dt.date.fromisoformat(day)
    return (d - dt.date(2000, 1, 1)).days * 1440 + int(t[:2]) * 60 + int(t[3:])


def shifted_later(key, reminded):
    """True if we already reminded an outage starting up to 60 min earlier: DTEK just moved
    the same outage later (e.g. 22:30 -> 23:00). Moved *earlier* is new info, so not here."""
    m = key_minutes(key)
    return any(0 < m - key_minutes(r) <= 60 for r in reminded)


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
        if 0 < lead <= REMIND_MIN and key not in reminded and not shifted_later(key, reminded):
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


def picture(ref, days, today, update_stamp, direct=False):
    """Mirror's day table (same commit) -> own drawing -> mirror's full table -> no picture.
    With data read directly from DTEK the mirror's pictures can't be trusted: own drawing only."""
    if os.environ.get("NO_IMAGE") or os.environ.get("SOURCE_FILE"):
        return None
    if direct:
        try:
            return render_image(days, today, update_stamp)
        except Exception as e:
            print(f"::warning::own drawing failed: {e}")
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
    direct = False
    if problem:                                   # mirror broken -> try DTEK's site ourselves
        scraped = scrape_dtek()
        scraped_days = group_days(scraped, today) if scraped else {}
        if scraped_days:
            print(f"::notice::mirror problem ({problem}); using DTEK site directly")
            src, days, problem, direct = scraped, scraped_days, None, True
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
        # Edits to the past or the current half-hour (DTEK sometimes rewrites an outage that
        # already started) can't be acted on: store them silently, report only the future.
        cut = now.hour * 2 + (1 if now.minute >= 30 else 0) + 1
        view, meaningful = dict(sent), []
        for d in changed:
            entry = sent.get(d.isoformat())
            if not entry:
                meaningful.append(d)                 # newly published day
                continue
            prev = prev_states(entry)
            c = cut if d == today else 0
            merged = [days[d][i] if i < c else prev[i] for i in range(48)]
            if merged != days[d]:
                meaningful.append(d)
                view[d.isoformat()] = {**entry, "off": off_list(merged)}
            else:                                    # past-only edit: show the day as unchanged
                view[d.isoformat()] = {**entry, "off": off_list(days[d])}
        # FORCE comes from the workflow input: manual "Run workflow" -> true, external
        # 15-min trigger -> false. Without FORCE, any manual dispatch forces (old workflow).
        if "FORCE" in os.environ:
            force = os.environ["FORCE"].lower() in ("1", "true")
        else:
            force = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        # one message per day, chronological; the picture (both days) goes with the first one
        if not sent:
            plan = [(d, "show") for d in sorted(days)]                 # very first run
        else:
            plan = []
            for d in sorted(meaningful):
                iso = d.isoformat()
                kind = ("new" if iso not in sent else
                        "change" if view[iso]["off"] != off_list(days[d]) else "maybe")
                plan.append((d, kind))
            if force and not plan:
                plan = [(d, "show") for d in sorted(days)]
        # morning digest: "Графік на сьогодні", unless today's news already went out this run
        digest_due = (DIGEST_HOUR <= now.hour < 12 and state.get("digest") != today.isoformat()
                      and today in days)
        if digest_due and all(d != today for d, _ in plan):
            plan.insert(0, (today, "digest"))

        stamp = (src.get("fact") or {}).get("update", "—")
        photo = picture(ref, days, today, stamp, direct) if plan else None
        delivered = set()
        for d, kind in plan:
            prev = view.get(d.isoformat(), {}).get("off")
            if send(day_message(kind, d, days[d], today, prev, stamp, direct), now, photo=photo):
                delivered.add(d)
                photo = None
            else:
                failed.append(f"schedule {d.isoformat()}")
        if digest_due and today in delivered:      # any delivered message about today counts
            state["digest"] = today.isoformat()
        # remember every change that was delivered or didn't need a message (past-only edits)
        for d in changed:
            if d in delivered or d not in meaningful:
                sent[d.isoformat()] = {"hash": schedule_hash(days[d]), "off": off_list(days[d]),
                                       "s": encode_states(days[d])}

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
