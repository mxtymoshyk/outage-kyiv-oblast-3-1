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
SRC = f"https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{REGION}.json"
DAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
Q = GROUP.replace("GPV", "")

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


def grid(states):
    """Two rows of 12 hours: 🟩 power · 🟥 none · 🟧 half-hour off · 🟨 possible."""
    cells = []
    for h in range(24):
        a, b = states[2 * h], states[2 * h + 1]
        if a == b == "off":
            cells.append("🟥")
        elif "off" in (a, b):
            cells.append("🟧")
        elif "maybe" in (a, b):
            cells.append("🟨")
        else:
            cells.append("🟩")
    return f"<code>00</code> {''.join(cells[:12])}\n<code>12</code> {''.join(cells[12:])}"


def off_list(states):
    return [[a, b] for s, a, b in runs(states) if s == "off"]


# ---------- message parts ----------
def now_line(today, tomorrow, now):
    """Current status from today's schedule (+tomorrow for overnight)."""
    line = today + (tomorrow or ["on"] * 48)
    idx = now.hour * 2 + (1 if now.minute >= 30 else 0)
    mins_now = now.hour * 60 + now.minute
    cur = line[idx]
    j = idx
    while j < len(line) and line[j] == cur:
        j += 1
    when = lambda k: f"{hm(k, True)}{' завтра' if k >= 48 else ''} (через {until(k * 30 - mins_now)})"
    if cur == "off":
        return f"🔴 <b>Зараз без світла</b> · увімкнуть о {when(j)}"
    if cur == "maybe":
        return "🟡 <b>Зараз можливе відключення</b>"
    nxt = next((k for k in range(idx, len(line)) if line[k] == "off"), None)
    if nxt is None:
        return "🟢 <b>Зараз світло є</b> · відключень далі не заплановано"
    return f"🟢 <b>Зараз світло є</b> · вимкнуть о {when(nxt)}"


def day_block(date, states, today_date, prev_off):
    label = "Сьогодні" if date == today_date else "Завтра" if date > today_date else str(date)
    offs = off_list(states)
    total = sum(b - a for a, b in offs)
    head = f"📅 <b>{label}, {DAYS_UA[date.weekday()]} {date:%d.%m}</b>"
    head += f" · без світла {dur(total)}" if total else ""
    if prev_off is not None and prev_off != offs:
        head += " ✏️"
    if not any(s != "on" for s in states):
        return head + "\n🟢 Відключень не заплановано"

    lines = [head, grid(states)]
    for s, a, b in runs(states):
        if s == "off":
            lines.append(f"🔴 {hm(a)}–{hm(b)} · {dur(b - a)}")
        elif s == "maybe":
            lines.append(f"🟡 {hm(a)}–{hm(b)} · можливо")
    on = [f"{hm(a)}–{hm(b)}" for s, a, b in runs(states) if s == "on"]
    if on:
        lines.append("🟢 Світло: " + ", ".join(on))
    if prev_off is not None and prev_off != offs:
        was = ", ".join(f"{hm(a)}–{hm(b)}" for a, b in prev_off) or "без відключень"
        lines.append(f"↩️ Було: <s>{was}</s>")
    return "\n".join(lines)


# ---------- telegram ----------
def is_quiet(now):
    if not QUIET:
        return False
    a, b = (int(x) for x in QUIET.split("-"))
    return (now.hour >= a or now.hour < b) if a > b else (a <= now.hour < b)


def send(text, now):
    silent = is_quiet(now)
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print(f"---- DRY RUN{' (silent)' if silent else ''} ----\n{text}\n")
        return
    # TG_CHAT_ID may hold several ids separated by commas: "123,456,-100789"
    ok = 0
    for cid in [c.strip() for c in chat.split(",") if c.strip()]:
        data = urllib.parse.urlencode({
            "chat_id": cid, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        }).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                        data=data, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            body = {"ok": False, "description": e.read().decode(errors="replace")}
        if body.get("ok"):
            ok += 1
        else:
            # one bad id must not block the others (or cause resends to them)
            print(f"::warning::Chat …{cid[-3:]} not delivered: {body.get('description')}")  # repo is public: never log full ids
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
        title = "Оновлення графіка" if changed and not first else "Графік"
        parts = [f"⚡ <b>{title} · черга {Q}</b>",
                 now_line(t_today, t_tom, now)]
        for d in sorted(days):
            prev = None if first else sent.get(d.isoformat(), {}).get("off")
            parts.append(day_block(d, days[d], today, prev))
        upd = fact.get("update", "—")
        parts.append(f"<i>🟧 пів години без світла · 🟨 можливо\nДТЕК · оновлено {upd}</i>")
        send("\n\n".join(parts), now)
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
                msg = (f"⏰ <b>Через {until(lead)} вимкнуть світло</b>\n"
                       f"🔴 {hm(a, True)}–{end} · {dur(b - a)} без світла\n"
                       f"🔋 Час зарядити батарею й павербанки\n"
                       f"<i>Черга {Q}</i>")
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
