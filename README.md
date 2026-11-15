# Outage 3.1 → Telegram

Checks the DTEK Kyiv-oblast schedule for queue 3.1 every 10 minutes and sends to Telegram:

- **Schedule**: when today's or tomorrow's schedule changes, or tomorrow's is published. Sent as a photo (the same DTEK table image that Svitlobot posts) with a caption: what changed, a monospace timeline for the days, and exact times. If the image is unavailable, the text is sent without it.
- **Reminder** about 1 hour before each outage, including ones that run past midnight.
- Messages between 23:00 and 07:00 arrive **silently** (no sound).
- **Alert if the data source breaks** (no updates for 3+ hours, or DTEK's site blocks the parser) and a follow-up when it recovers. Silence never means "no changes" while the data is broken.
- **Calendar** `outage.ics`: outages appear as events in iPhone/Mac Calendar, with an alarm 30 minutes before. History is kept for 2 weeks.

## Calendar subscription
URL: `webcal://raw.githubusercontent.com/mxtymoshyk/outage-kyiv-oblast-3-1/main/outage.ics`
- **iPhone:** Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar → paste the URL → Next → turn **off** "Remove Alerts" → Save.
- **Mac:** Calendar → File → New Calendar Subscription → URL → Location **iCloud** (it then shows up on iPhone too) → Auto-refresh: every 15 minutes → uncheck Alerts under Remove.

Data: [outage-data-ua](https://github.com/Baskerville42/outage-data-ua), a mirror of dtek-krem.com.ua that updates every few minutes.

## Setup
1. Telegram: @BotFather → `/newbot` → token.
2. Send the bot a message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Repo → Settings → Secrets and variables → Actions: add `TG_TOKEN` and `TG_CHAT_ID`. For several recipients, separate the IDs with commas: `123456789,987654321`. Each person must press Start in the bot first.
4. Actions → "Outage schedule → Telegram" → Run workflow.

## Settings (`env:` in `.github/workflows/outage.yml`)
| Variable | Default | What it does |
|---|---|---|
| `GROUP` | `GPV3.1` | Queue |
| `REMIND_MIN` | `60` | How many minutes before an outage to send the reminder (`0` = off) |
| `QUIET` | `23-7` | Hours when messages arrive without sound (empty = always with sound) |

## Legend
Timeline: `·` power · `█` no power · `▌` / `▐` no power in the first / second half of the hour · `░` possible outage. ✏️ day changed · 🆕 new day
