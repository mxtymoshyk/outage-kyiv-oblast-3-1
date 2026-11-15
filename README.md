# Outage 3.1 → Telegram

Checks the DTEK Kyiv-oblast schedule for queue 3.1 every 10 minutes and sends to Telegram:

- **Schedule**: when today's or tomorrow's schedule changes, or tomorrow's is published. Shows the current status, a 24-hour grid, the outage and power windows, and what changed (↩️ Було).
- **Reminder** about 1 hour before each outage, including ones that run past midnight.
- Messages between 23:00 and 07:00 arrive **silently** (no sound).

Data: [outage-data-ua](https://github.com/Baskerville42/outage-data-ua), a mirror of dtek-krem.com.ua that updates every few minutes.

## Setup
1. Telegram: @BotFather → `/newbot` → token.
2. Send the bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Repo → Settings → Secrets and variables → Actions: add `TG_TOKEN` and `TG_CHAT_ID`.
4. Actions → "Outage schedule → Telegram" → Run workflow.

## Settings (`env:` in `.github/workflows/outage.yml`)
| Variable | Default | What it does |
|---|---|---|
| `GROUP` | `GPV3.1` | Queue |
| `REMIND_MIN` | `60` | How many minutes before an outage to send the reminder (`0` = off) |
| `QUIET` | `23-7` | Hours when messages arrive without sound (empty = always with sound) |

## Legend
🟩 power · 🟥 no power · 🟧 out for half an hour · 🟨 possible outage · ✏️ day changed
