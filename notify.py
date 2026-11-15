#!/usr/bin/env python3
"""DTEK Kyiv-oblast outage schedule -> Telegram.

Two kinds of messages:
  * schedule  - when today's/tomorrow's schedule for the group changes
                (or tomorrow's is published); shows what changed
  * reminder  - ~REMIND_MIN minutes before each planned outage
Messages between QUIET hours are delivered silently. Stdlib only.

Env:
  TG_TOKEN, TG_CHAT_ID  Telegram bot token / chat id (empty = dry run)
  GROUP        default GPV3.1
  REGION       default kyiv-region
  REMIND_MIN   reminder lead time in minutes, default 60 (0 = off)
  QUIET        silent hours "23-7" (empty = never silent)
  STATE_FILE   default state.json
  SOURCE_FILE  local JSON instead of download (tests)
  TEST_NOW     ISO datetime to pretend "now" (tests)
  FORCE        send the schedule even if nothing changed
"""
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
GROUP = os.environ.get("GROUP", "GPV3.1")
REGION = os.environ.get("REGION", "kyiv-region")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60") or 0)
QUIET = os.environ.get("QUIET", "23-7")
STALE_MIN = int(os.environ.get("STALE_MIN", "180"))   # source older than this = broken
ICS_FILE = os.environ.get("ICS_FILE", "outage.ics")
ICS_KEEP_DAYS = 14
BASE = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main"
SRC = f"{BASE}/data/{REGION}.json"
DAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
Q = GROUP.replace("GPV", "")
IMG = f"{BASE}/images/{REGION}/gpv-{Q.replace('.', '-')}.png"   # same table image Svitlobot posts

# hour-slot value -> (first half, second half)
SLOT = {
    "yes": ("on", "on"), "no": ("off", "off"),
    "first": ("off", "on"), "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"), "msecond": ("on", "maybe"),
}


# ---------- helpers ----------
def now_kyiv():
    t = os.environ.get("TEST_NOW")
    return dt.datetime.fromisoformat(t).replace(tzinfo=KYIV) if t else dt.datetime.now(KYIV)


def load_source():
    if os.environ.get("SOURCE_FILE"):
        with open(os.environ["SOURCE_FILE"], encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(SRC, headers={"User-Agent": "outage-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def half_hours(slots):
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


# ---------- message parts ----------
def span_list(idx):
    """sorted half-hour indices -> 'HH:MM–HH:MM, …'"""
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


def day_label(date, today):
    return "Сьогодні" if date == today else "Завтра" if date == today + dt.timedelta(days=1) else f"{date:%d.%m}"


def day_block(date, states, today, prev_off=None, is_new=False):
    """One self-contained block per day: header · total, times, and what changed."""
    offs = off_list(states)
    total = sum(b - a for a, b in offs)
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


def title(days, today, changed, fresh, sent, first):
    """First line(s) = the news, which is what a lock-screen preview shows."""
    def total(d):
        return sum(b - a for a, b in off_list(days[d]))

    def word(d):
        return {today: "сьогодні", today + dt.timedelta(days=1): "завтра"}.get(d, f"{d:%d.%m}")

    def amount(d):
        t = total(d)
        return f"без світла {dur(t)}" if t else "без відключень"

    if first or not changed:
        return None        # no news: the day blocks themselves come first
    news = []
    for d in sorted(changed):
        prev = sent.get(d.isoformat(), {}).get("off")
        if d in fresh:
            news.append(f"🆕 <b>Графік на {word(d)}</b> · {amount(d)}")
        elif prev != off_list(days[d]):
            net = total(d) - sum(b - a for a, b in prev)
            delta = (f"без світла на {dur(abs(net))} {'більше' if net > 0 else 'менше'}"
                     if net else "час без світла той самий")
            news.append(f"✏️ <b>Змінився графік на {word(d)}</b> · {delta}")
    return "\n".join(news) or "🟡 <b>Оновлено можливі відключення</b>"


# ---------- telegram ----------
def is_quiet(now):
    if not QUIET:
        return False
    a, b = (int(x) for x in QUIET.split("-"))
    return (now.hour >= a or now.hour < b) if a > b else (a <= now.hour < b)


def fetch_image():
    if os.environ.get("NO_IMAGE"):
        return None
    try:
        req = urllib.request.Request(IMG, headers={"User-Agent": "outage-bot"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
    except Exception as e:
        print(f"::warning::image not loaded: {e}")
        return None
    return crop_image(data)


def crop_image(png):
    """Drop the weekly "possible outages" table (noise once DTEK publishes day-ahead
    schedules). Layout: title, day table, [gap] weekly table, [gap] legend.
    Any surprise in the layout -> send the original image."""
    try:
        try:
            from PIL import Image
        except ImportError:
            if not os.environ.get("GITHUB_ACTIONS"):
                return png
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--user",
                            "--break-system-packages", "pillow"], check=True, timeout=120)
            import site, importlib
            sys.path.append(site.getusersitepackages())
            importlib.invalidate_caches()
            from PIL import Image
        import io
        im = Image.open(io.BytesIO(png)).convert("RGB")
        w, hgt = im.size
        px = im.load()
        bg = px[5, 5]
        empty = [all(px[x, y] == bg for x in range(0, w, 3)) for y in range(hgt)]
        gaps, y = [], 0
        while y < hgt:
            if empty[y]:
                s0 = y
                while y < hgt and empty[y]:
                    y += 1
                if y - s0 >= 20:
                    gaps.append((s0, y))
            else:
                y += 1
        # inner gaps (not the top/bottom margins) that separate big blocks
        inner = [g for g in gaps if g[0] > 0 and g[1] < hgt and g[1] - g[0] >= 30]
        if len(inner) < 2:
            return png
        cut = inner[0][0] + (inner[0][1] - inner[0][0]) // 2      # after the day table
        legend = inner[-1][0] + (inner[-1][1] - inner[-1][0]) // 2  # before the legend
        if not (0.15 * hgt < cut < legend < hgt):
            return png
        out = Image.new("RGB", (w, cut + hgt - legend), bg)
        out.paste(im.crop((0, 0, w, cut)), (0, 0))
        out.paste(im.crop((0, legend, w, hgt)), (0, cut))
        buf = io.BytesIO()
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"::warning::crop failed, sending full image: {e}")
        return png


def tg(method, fields, file=None):
    token = os.environ["TG_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    if file:  # multipart upload
        boundary = "outagebot" + hashlib.sha1(file).hexdigest()[:16]
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"schedule.png\"\r\n"
                 "Content-Type: image/png\r\n\r\n").encode() + file + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode(errors="replace")}


def send(text, now, photo=None):
    """Text message, or a photo with the text as caption (falls back to text on any photo error)."""
    silent = is_quiet(now)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        tag = f" + photo {len(photo) // 1024} KB" if photo else ""
        print(f"---- DRY RUN{' (silent)' if silent else ''}{tag} ----\n{text}\n")
        return
    common = {"parse_mode": "HTML", "disable_notification": "true" if silent else "false"}
    file_id = None
    ok = 0
    # TG_CHAT_ID may hold several ids separated by commas: "123,456,-100789"
    for cid in [c.strip() for c in chat.split(",") if c.strip()]:
        body = {"ok": False}
        if photo:
            if file_id:
                body = tg("sendPhoto", {**common, "chat_id": cid, "caption": text, "photo": file_id})
            else:
                body = tg("sendPhoto", {**common, "chat_id": cid, "caption": text}, file=photo)
                if body.get("ok"):
                    file_id = body["result"]["photo"][-1]["file_id"]
            if not body.get("ok"):
                print(f"::warning::photo to …{cid[-3:]} failed, sending text: {body.get('description')}")
        if not body.get("ok"):
            body = tg("sendMessage", {**common, "chat_id": cid, "text": text, "disable_web_page_preview": "true"})
        if body.get("ok"):
            ok += 1
        else:
            # one bad id must not block the others (or cause resends to them); repo is public: never log full ids
            print(f"::warning::Chat …{cid[-3:]} not delivered: {body.get('description')}")
    if not ok:
        sys.exit("Telegram: message not delivered to any chat")


# ---------- calendar feed (.ics) ----------
def ics_events(cal):
    """cal: {iso_date: states48} -> merged events across midnight [(state, start_dt, end_dt)]."""
    evs = []
    for iso in sorted(cal):
        base = dt.datetime.combine(dt.date.fromisoformat(iso), dt.time(), KYIV)
        for st, a, b in runs(cal[iso]):
            if st == "on":
                continue
            start, end = base + dt.timedelta(minutes=30 * a), base + dt.timedelta(minutes=30 * b)
            if evs and evs[-1][0] == st and evs[-1][2] == start:   # glue 23:00-24:00 + 00:00-03:00
                evs[-1] = (st, evs[-1][1], end)
            else:
                evs.append((st, start, end))
    return evs


def write_ics(cal):
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
            f"DTSTAMP:{utc(a)}",
            f"DTSTART:{utc(a)}", f"DTEND:{utc(b)}",
            f"SUMMARY:{'🔴 Без світла' if off else '🟡 Можливе відключення'}",
            f"DESCRIPTION:Черга {Q} · графік ДТЕК",
            "TRANSP:OPAQUE" if off else "TRANSP:TRANSPARENT",
        ]
        if off:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Скоро вимкнуть світло",
                      "TRIGGER:-PT30M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    try:
        with open(ICS_FILE, encoding="utf-8", newline="") as f:
            if f.read() == body:
                return
    except FileNotFoundError:
        pass
    with open(ICS_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(body)


# ---------- source health ----------
def source_problem(src, now):
    """None if the mirror is fresh, else a short reason."""
    if src is None:
        return "не вдалося завантажити дані"
    st = src.get("lastUpdateStatus") or {}
    try:
        upd = dt.datetime.fromisoformat(src["lastUpdated"].replace("Z", "+00:00"))
        age = (now - upd).total_seconds() / 60
    except (KeyError, ValueError):
        return "невідомий формат даних"
    if age > STALE_MIN:
        return f"дані не оновлювались {until(age)}"
    if st.get("ok") is False:
        return f"сайт ДТЕК не віддає графік (код {st.get('code')})"
    return None

# ---------- main ----------
def main():
    now = now_kyiv()
    today = now.date()
    try:
        src = load_source()
    except Exception as e:  # network / JSON errors: report via health check, don't crash
        print(f"::warning::source load failed: {e}")
        src = None

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    # 0) source health: alert once if broken for 1h+, and once when it recovers
    problem = source_problem(src, now)
    health = state.get("health", {})
    if problem:
        since = health.get("since") or now.isoformat(timespec="minutes")
        health["since"] = since
        age = (now - dt.datetime.fromisoformat(since)).total_seconds() / 60
        if age >= 60 and not health.get("alerted"):
            send(f"⚠️ <b>Бот не бачить актуального графіка</b>\n{problem}.\n"
                 f"Поки не відновиться, звіряйся з dtek-krem.com.ua\n<i>Черга {Q}</i>", now)
            health["alerted"] = True
    else:
        if health.get("alerted"):
            send(f"✅ <b>Дані про графік знову надходять</b>\n<i>Черга {Q}</i>", now)
        health = {}
    state["health"] = health
    if src is None:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        return

    fact = src.get("fact") or {}
    days = {}
    for ts, groups in (fact.get("data") or {}).items():
        d = dt.datetime.fromtimestamp(int(ts), KYIV).date()
        if d >= today and GROUP in groups:
            days[d] = half_hours(groups[GROUP])
    if not days:
        print(f"No data for {GROUP}; skipping.")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        return

    sent = state.get("days", {})          # iso date -> {"hash", "off"}
    reminded = set(state.get("reminded", []))
    h = lambda s: hashlib.sha1("".join(s).encode()).hexdigest()[:12]

    tomorrow = today + dt.timedelta(days=1)
    t_today = days.get(today, ["on"] * 48)
    t_tom = days.get(tomorrow)

    # 1) schedule message on change
    changed = [d for d, s in days.items() if sent.get(d.isoformat(), {}).get("hash") != h(s)]
    # manual "Run workflow" in GitHub always sends the current schedule
    force = os.environ.get("FORCE") or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if changed or force:
        first = not sent
        fresh = [d for d in changed if d.isoformat() not in sent]   # newly published days
        news = title(days, today, changed, fresh, sent, first)
        parts = [news] if news else []
        for d in sorted(days):                       # chronological: today, then tomorrow
            prev = None if first else sent.get(d.isoformat(), {}).get("off")
            parts.append(day_block(d, days[d], today, prev, is_new=(d in fresh and not first)))
        parts.append(f"<i>Черга {Q} · ДТЕК, оновлено {fact.get('update', '—')}</i>")
        text = "\n\n".join(parts)
        # caption limit is 1024 visible chars; long texts go without the picture
        visible = len(text) - sum(len(t) for t in ("<b>", "</b>", "<i>", "</i>", "<pre>", "</pre>") for _ in range(text.count(t)))
        send(text, now, photo=fetch_image() if visible <= 1000 else None)
        for d in changed:
            sent[d.isoformat()] = {"hash": h(days[d]), "off": off_list(days[d])}

    # 2) reminders before each outage (today + overnight into tomorrow)
    if REMIND_MIN:
        line = t_today + (t_tom or ["on"] * 48)
        mins_now = now.hour * 60 + now.minute
        for s, a, b in runs(line):
            if s != "off" or a == 0:
                continue
            lead = a * 30 - mins_now
            key = f"{(today if a < 48 else tomorrow).isoformat()}@{hm(a, True)}"
            if 0 < lead <= REMIND_MIN and key not in reminded:
                end = f"{hm(b)}{' завтра' if b > 48 else ''}"
                msg = (f"⏰ <b>О {hm(a, True)} вимкнуть світло</b> · через {until(lead)}\n"
                       f"До {end} · {dur(b - a)} без світла")
                send(msg, now)
                reminded.add(key)

    # keep state small: only today and later
    # 3) calendar feed: current days + last 2 weeks of history
    cal = state.get("cal", {})
    cal.update({d.isoformat(): "".join("ox?"[["on", "off", "maybe"].index(x)] for x in s)
                for d, s in days.items()})
    cutoff = (today - dt.timedelta(days=ICS_KEEP_DAYS)).isoformat()
    cal = {k: v for k, v in cal.items() if k >= cutoff}
    write_ics({k: [{"o": "on", "x": "off", "?": "maybe"}[c] for c in v] for k, v in cal.items()})

    keep = {d.isoformat() for d in days}
    state = {
        "days": {k: v for k, v in sent.items() if k in keep},
        "reminded": sorted(k for k in reminded if k[:10] >= today.isoformat()),
        "cal": cal,
        "health": state.get("health", {}),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print("changed:", [d.isoformat() for d in changed])


if __name__ == "__main__":
    main()
