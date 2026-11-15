#!/usr/bin/env python3
"""DTEK Kyiv-oblast outage schedule -> Telegram.

Sends a message only when the schedule for your group changes
(or when tomorrow's schedule is published). Stdlib only.

Env:
  TG_TOKEN    Telegram bot token from @BotFather   (empty = dry run, prints)
  TG_CHAT_ID  chat / user / channel id to send to
  GROUP       queue id, default GPV3.1
  REGION      data file, default kyiv-region
  STATE_FILE  default state.json
  SOURCE_FILE optional local JSON instead of downloading (for tests)
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
SRC = f"https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{REGION}.json"
DAYS_UA = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

# hour-slot value -> (first half, second half); "off" / "maybe" / "on"
SLOT = {
    "yes": ("on", "on"),
    "no": ("off", "off"),
    "first": ("off", "on"),
    "second": ("on", "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", "on"),
    "msecond": ("on", "maybe"),
}


def load_source():
    if os.environ.get("SOURCE_FILE"):
        with open(os.environ["SOURCE_FILE"], encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(SRC, headers={"User-Agent": "outage-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def half_hours(slots):
    """24 hour slots -> 48 half-hour states."""
    out = []
    for h in range(1, 25):
        out.extend(SLOT.get(slots.get(str(h), "yes"), ("on", "on")))
    return out


def fmt(i):
    return "24:00" if i == 48 else f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def intervals(states):
    """Merge consecutive half-hours of the same non-'on' state."""
    res, i = [], 0
    while i < 48:
        s = states[i]
        j = i
        while j < 48 and states[j] == s:
            j += 1
        if s != "on":
            res.append((s, i, j))
        i = j
    return res


def day_block(ts, slots, today_ts):
    d = dt.datetime.fromtimestamp(int(ts), KYIV).date()
    label = "Сьогодні" if int(ts) == int(today_ts) else "Завтра" if int(ts) > int(today_ts) else "Вчора"
    head = f"📅 <b>{label}, {DAYS_UA[d.weekday()]} {d:%d.%m}</b>"
    ivs = intervals(half_hours(slots))
    if not ivs:
        return head + "\n🟢 Відключень не заплановано"
    lines = [head]
    off_total = 0
    for s, a, b in ivs:
        icon = "🔴" if s == "off" else "🟡"
        note = "" if s == "off" else "  <i>можливо</i>"
        lines.append(f"{icon} {fmt(a)}–{fmt(b)}{note}")
        if s == "off":
            off_total += b - a
    if off_total:
        h, m = divmod(off_total * 30, 60)
        lines.append(f"⏱ Без світла: {h} год" + (f" {m} хв" if m else ""))
    return "\n".join(lines)


def send(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("---- DRY RUN (no TG_TOKEN/TG_CHAT_ID) ----\n" + text)
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        body = json.load(r)
    if not body.get("ok"):
        sys.exit(f"Telegram error: {body}")


def main():
    src = load_source()
    fact = src.get("fact") or {}
    days = fact.get("data") or {}
    today_ts = int(fact.get("today", 0))
    if not days:
        print("No data in source; skipping.")
        return

    # Only today and future days for our group
    mine = {ts: v[GROUP] for ts, v in days.items() if int(ts) >= today_ts and GROUP in v}
    if not mine:
        print(f"{GROUP} not found; skipping.")
        return

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    sent = state.get("sent", {})

    def h(slots):
        return hashlib.sha1(json.dumps(slots, sort_keys=True).encode()).hexdigest()

    changed = [ts for ts, slots in mine.items() if sent.get(ts) != h(slots)]
    if not changed and not os.environ.get("FORCE"):
        print("No changes.")
        return

    first_run = not sent
    title = "Графік відключень" if first_run else "Оновлення графіка"
    q = GROUP.replace("GPV", "")
    parts = [f"⚡ <b>{title} · черга {q}</b>"]
    for ts in sorted(mine, key=int):
        block = day_block(ts, mine[ts], today_ts)
        if ts in changed and not first_run:
            block = block.replace("</b>", "</b> ✏️", 1)
        parts.append(block)
    parts.append(f"<i>ДТЕК, оновлено {fact.get('update', '—')}</i>")
    send("\n\n".join(parts))

    # keep only hashes for days still present (drops past days)
    state["sent"] = {ts: h(slots) for ts, slots in mine.items()}
    state["lastSent"] = dt.datetime.now(KYIV).isoformat(timespec="seconds")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    print("Sent; days changed:", changed)


if __name__ == "__main__":
    main()
