# SIRI Copenhagen appointment watcher

Polls the SIRI / International House Copenhagen booking pages and sends you a
Telegram message when appointment slots appear. Two locations are supported, each
with its own booking system:

| Key | Location | System | Service | Default poll |
| --- | --- | --- | --- | --- |
| `nyropsgade` | Nyropsgade (HQ) | FrontDeskSuite | CPR / residence card | 60s |
| `carljacobsens` | Carl Jacobsens Vej, Valby | CleverQ | Applying under EU regulations | 180s |

Both run in a single process by default, each with its own state file, so a
booking slot at one location never suppresses or fakes an alert for the other.

## How it works

The two sites need completely different approaches.

### FrontDeskSuite (Nyropsgade)

An ASP.NET MVC flow that renders availability only when the request carries a
server-side `ReserveTimeState` cookie. So every poll replays the real flow from a
clean session:

```
GET /kk/SIRI%20Copenhagen/ReserveTime/StartReservation?...   -> 302, sets ReserveTimeState
GET /kk/SIRI%20Copenhagen/ReserveTime/TimeSelection?...      -> 200, the availability page
```

The page is then parsed for bookable slots. A date counts as open only if it
renders `.time button` elements, so changes to the site's "no availability"
wording cannot cause false positives. If the flow ever breaks, the watcher logs
an error and retries with backoff instead of reporting a false "nothing new".

### CleverQ (Carl Jacobsens Vej)

A JavaScript single-page app, so there is no server-rendered availability to
parse. Instead the watcher uses the same public API the booking page itself calls:

```
GET /public/appointments/jacobsens/index.html      -> sets _scandic_session cookie, exposes CSRF token
GET /api/external/v4/sites/3/appointments/available_days
GET /api/external/v4/sites/3/appointments/available_time_slots?day=YYYY-MM-DD
```

The CSRF token from the page is sent as `X-CSRF-TOKEN`, and no API key is
required. A slot is treated as bookable only when its `available` count is greater
than zero — note that an open day still lists slots with `available: 0`, and those
are *not* bookable.

This site has `appointments_use_subtasks: true`, so the request must name the real
subtask (`subtask_id=46`, "Jeg vil ansøge om ophold efter EU-reglerne"). This is
not optional and it fails quietly: with an unknown subtask id the API still answers
`200 OK`, reports *more* days as open, and marks every slot `available: 0` — which
looks exactly like a fully booked office. If availability ever drops to zero while
the log reports open days, re-check `CQ_SUBTASK_ID` against
`https://scandic.cleverq.de/api/external/v4/sites/3`, which lists every service and
subtask for this location. Ids 46 (residence), 47 (family reunification) and 48
(permanent residence) currently return identical availability.

The site also clamps its own booking window to roughly 40 days, so `to_day` beyond
that changes nothing.

This is also why the default interval is 180s rather than 60s: each poll costs
one session request, one `available_days` request, and one request per open day
(2 + N in total).

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
uv run python watcher.py                          # watch all sites continuously
uv run python watcher.py --once                   # single poll of each site, then exit
uv run python watcher.py --site carljacobsens     # watch just one site
uv run python watcher.py --site nyropsgade,carljacobsens
uv run python watcher.py --interval 120           # override both intervals
uv run python watcher.py --heartbeat-hours 6      # more frequent status pings
uv run python watcher.py --max-runtime 55         # exit cleanly after 55 min
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | required, from @BotFather |
| `TELEGRAM_CHAT_ID` | — | required |
| `SITES` | `all` | comma-separated site keys to watch |
| `POLL_INTERVAL` | per site | overrides every site's interval, in seconds |
| `HEARTBEAT_HOURS` | `24` | hours between status pings |
| `MAX_RUNTIME` | `0` | minutes before a clean exit (`0` = forever) |
| `STATE_DIR` | `.` | directory holding `state-<site>.json` |

Each site keeps its own `state-<site>.json`, so its first run baselines
independently and its slots are tracked separately. Delete a site's file to reset
just that site.

## Notification behaviour

- **First run** sends a baseline message with whatever is currently open, so you
  know immediately that the bot works. It does *not* alert on those.
- **New slots** — each distinct `date + time` you have never seen before alerts
  exactly once, ever. Unchanged slots stay silent, so there is no repeat spam.
- **Heartbeat** — every 24h, a "N slots open right now" status ping, so silence
  is distinguishable from a dead watcher.
- **Carl Jacobsens Vej reports only the earliest date.** That site releases
  hundreds of 5-minute slots at once, so its messages carry just the first
  bookable date instead of the full list — 282 characters rather than 2,507. Every
  message there reports the best date *currently* on offer, including new-slot
  alerts, so a late addition never masks an earlier date you could already have
  booked. The Nyropsgade (CPR) messages still list every date and time.

`state-<site>.json` is what makes slots "seen before". Delete one to reset that
site and get a fresh baseline.

## Running on GitHub Actions

`.github/workflows/watch.yml` runs the watcher on GitHub-hosted runners.

**Setup:** add two repository secrets under *Settings → Secrets and variables →
Actions*: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Then let it run, or use
*Run workflow* to start one immediately.

**How state survives an ephemeral runner.** Runners are wiped between jobs, so
the state files would be lost and every slot would re-alert on each restart. The
workflow commits them to a dedicated `watcher-state` branch and force-pushes at
the end of every run (even on failure), then restores them at the start. A
separate branch is used so branch protection on your default branch cannot block
it. Verified: a slot seen in one job stays silent in the next, and still alerts
if newly relevant.

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

- Every message starts with the location name and ends with a booking link. For
  FrontDeskSuite this must be the `StartReservation` link, because the
  `TimeSelection` URL alone will not load for you. The watcher deliberately does
  not book anything — you complete the booking yourself.
- The sites warn that concurrent booking sessions are unsupported, so do not run
  multiple copies of this watcher.
- The default intervals are a deliberate compromise. Poll much faster and you
  risk an IP block, which would cost you exactly the alert you are waiting for.
  CleverQ is slower because each poll there costs far more requests.
- The CleverQ site caps its booking window at roughly 40 days, so a "no slots"
  status there does not mean the office has no appointments beyond that horizon.
