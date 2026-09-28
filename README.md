# SIRI Copenhagen appointment watcher

Polls the SIRI / International House Copenhagen CPR-booking page and sends you a
Telegram message when appointment slots appear.

## How it works

The site is an ASP.NET MVC flow that
renders availability only when the request carries a server-side
`ReserveTimeState` cookie.

So every poll replays the real flow from a clean session:

```
GET /kk/SIRI%20Copenhagen/ReserveTime/StartReservation?...   -> 302, sets ReserveTimeState
GET /kk/SIRI%20Copenhagen/ReserveTime/TimeSelection?...      -> 200, the availability page
```

The page is then parsed for bookable slots. A date counts as open only if it
renders `.time button` elements, so changes to the site's "no availability"
wording cannot cause false positives. If the flow ever breaks, the watcher logs
an error and retries with backoff instead of reporting a false "nothing new".

## Setup

**1. Create the Telegram bot**

- Message [@BotFather](https://t.me/BotFather) -> `/newbot` -> follow the prompts
- Copy the token it gives you

**2. Find your chat id**

- Send any message to your new bot (e.g. "hi")
- Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
- Find `"chat":{"id": ...}` in the response

**3. Configure**

```bash
cp .env.example .env
$EDITOR .env      # paste TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
```

**4. Run**

```bash
uv sync
uv run python watcher.py
```

## Usage

```bash
uv run python watcher.py                      # watch continuously (60s)
uv run python watcher.py --once               # single poll, then exit
uv run python watcher.py --interval 120       # slower
uv run python watcher.py --heartbeat-hours 6  # more frequent status pings
uv run python watcher.py --max-runtime 55     # exit cleanly after 55 min
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | required, from @BotFather |
| `TELEGRAM_CHAT_ID` | — | required |
| `POLL_INTERVAL` | `60` | seconds between polls |
| `HEARTBEAT_HOURS` | `24` | hours between status pings |
| `MAX_RUNTIME` | `0` | minutes before a clean exit (`0` = forever) |
| `STATE_FILE` | `state.json` | where seen slots are persisted |

## Notification behaviour

- **First run** sends a baseline message with whatever is currently open, so you
  know immediately that the bot works. It does *not* alert on those.
- **New slots** — each distinct `date + time` you have never seen before alerts
  exactly once, ever. Unchanged slots stay silent, so there is no repeat spam.
- **Heartbeat** — every 24h, a "N slots open right now" status ping, so silence
  is distinguishable from a dead watcher.

`state.json` is what makes slots "seen before". Delete it to reset the watcher
and get a fresh baseline.

## Running on GitHub Actions

`.github/workflows/watch.yml` runs the watcher on GitHub-hosted runners.

**Setup:** add two repository secrets under *Settings → Secrets and variables →
Actions*: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Then let it run, or use
*Run workflow* to start one immediately.

**How state survives an ephemeral runner.** Runners are wiped between jobs, so
`state.json` would be lost and every slot would re-alert on each restart. The
workflow commits it to a dedicated `watcher-state` branch and force-pushes at the
end of every run (even on failure), then restores it at the start. A separate
branch is used so branch protection on your default branch cannot block it.
Verified: a slot seen in one job stays silent in the next, and still alerts if
newly relevant.

**Read this before enabling it.** GitHub-hosted runners are billed by the
minute, and the free tier does not come close to covering continuous polling:

| Config | Rate | Coverage | Private repo, ~30 days |
| --- | --- | --- | --- |
| `ubuntu-latest`, 55 min of each hour (as shipped) | $0.006/min | ~92% | **~$238/mo** (2,000 min free) |
| `ubuntu-slim`, 14 min of each hour | $0.002/min | ~25% | **~$22/mo** |
| Any of the above, **public** repo | free | same | **$0** |

Standard runner usage is free and unlimited on public repositories, so a public
repo runs this indefinitely at no cost. On a private repo, prefer the
`ubuntu-slim` variant: change `runs-on` to `ubuntu-slim`, set `run_minutes` to
`14`, and keep `timeout-minutes` at `70`. Note `ubuntu-slim` is hard-capped at
**15 minutes per job**, so it cannot run a long single job.

## Notes

- A message includes a `StartReservation` link, because the `TimeSelection` URL
  alone will not load for you. Open it and complete the booking yourself — the
  watcher deliberately does not book anything.
- The site warns that concurrent booking sessions are unsupported, so do not run
  multiple copies of this watcher.
- The default 60s interval is a deliberate compromise. Poll much faster and you
  risk an IP block, which would cost you exactly the alert you are waiting for.
