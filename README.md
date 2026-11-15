# Outage 3.1 → Telegram

Checks the DTEK Kyiv-oblast schedule for queue 3.1 every 10 minutes and sends a Telegram message whenever it changes, including when tomorrow's schedule is published.
Data source: [outage-data-ua](https://github.com/Baskerville42/outage-data-ua) (a mirror of dtek-krem.com.ua, updated every few minutes).

## Setup (5 min)
1. **Bot:** in Telegram, message @BotFather → `/newbot` → copy the token.
2. **Chat ID:** send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": …}`.
   (For a family group: add the bot to the group, write something there, and use that group's negative id.)
3. **Secrets:** in the repo, go to Settings → Secrets and variables → Actions → New secret:
   `TG_TOKEN` and `TG_CHAT_ID`.
4. **Test:** go to Actions → "Outage schedule → Telegram" → Run workflow. The first run always sends the current schedule.

## Change the group
Edit `GROUP: GPV3.1` in `.github/workflows/outage.yml` (for example `GPV3.2`).

## Legend
🔴 no power · 🟡 possible outage · ✏️ this day changed since the last message
