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
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
GROUP = os.environ.get("GROUP", "GPV3.1")
REGION = os.environ.get("REGION", "kyiv-region")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
REMIND_MIN = int(os.environ.get("REMIND_MIN", "60") or 0)
QUIET = os.environ.get("QUIET", "23-7")
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
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "disable_notification": "true" if silent else "false",
    }).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                data=data, timeout=30) as r:
        body = json.load(r)
    if not body.get("ok"):
        sys.exit(f"Telegram error: {body}")


# ---------- main ----------
def main():
    now = now_kyiv()
    today = now.date()
    src = load_source()
    fact = src.get("fact") or {}
    days = {}
    for ts, groups in (fact.get("data") or {}).items():
        d = dt.datetime.fromtimestamp(int(ts), KYIV).date()
        if d >= today and GROUP in groups:
            days[d] = half_hours(groups[GROUP])
    if not days:
        print(f"No data for {GROUP}; skipping.")
        return

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    sent = state.get("days", {})          # iso date -> {"hash", "off"}
    reminded = set(state.get("reminded", []))
    h = lambda s: hashlib.sha1("".join(s).encode()).hexdigest()[:12]

    tomorrow = today + dt.timedelta(days=1)
    t_today = days.get(today, ["on"] * 48)
    t_tom = days.get(tomorrow)

    # 1) schedule message on change
    changed = [d for d, s in days.items() if sent.get(d.isoformat(), {}).get("hash") != h(s)]
    if changed or os.environ.get("FORCE"):
        first = not sent
        parts = [f"⚡ <b>{'Графік' if first else 'Оновлення графіка'} · черга {Q}</b>",
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
    keep = {d.isoformat() for d in days}
    state = {
        "days": {k: v for k, v in sent.items() if k in keep},
        "reminded": sorted(k for k in reminded if k[:10] >= today.isoformat()),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print("changed:", [d.isoformat() for d in changed])


if __name__ == "__main__":
    main()
