#!/usr/bin/env python3
"""Watch SIRI Copenhagen appointment booking pages and alert on newly released slots.

Two independent booking systems are supported:

* **FrontDeskSuite** (Nyropsgade) is an ASP.NET MVC flow whose TimeSelection page
  is only rendered when the request carries a server-side ``ReserveTimeState``
  cookie. The page cannot be fetched directly, so every poll replays
  ``StartReservation`` from a clean cookie jar.
* **CleverQ** (Carl Jacobsens Vej) is a JSON API gated by a Rails session cookie
  plus a CSRF token, both issued by loading the booking page. Availability must
  then be queried one day at a time, so it costs more requests per poll.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("watcher")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SUPERSCRIPT_RE = re.compile(r"[\u00b2\u00b3\u00b9\u02b0-\u02ff\u1d2c-\u1dbf\u2070-\u209f]")
SPACE_RE = re.compile(r"\s+")
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:])")
CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')

DEFAULT_INTERVAL = 60.0
DEFAULT_HEARTBEAT_HOURS = 24.0
DEFAULT_TIMEOUT = 30.0
NOTIFY_TIMEOUT = 15.0

# --- FrontDeskSuite: SIRI Copenhagen, Nyropsgade 1 ---
FDS_BASE = "https://reservation.frontdesksuite.com"
FDS_SITE = "kk/SIRI%20Copenhagen"
FDS_PAGE_ID = "2dcd2a7a-e666-4cf2-86d2-e035a8a638ee"
FDS_BUTTON_ID = "a4b27a7d-15e6-4420-bc1e-176f58fe5f92"
FDS_START_URL = (
    f"{FDS_BASE}/{FDS_SITE}/ReserveTime/StartReservation"
    f"?pageId={FDS_PAGE_ID}&buttonId={FDS_BUTTON_ID}&culture=en&uiCulture=en"
)

# --- CleverQ: SIRI Copenhagen, Carl Jacobsens Vej 39 ---
CQ_BASE = "https://scandic.cleverq.de"
CQ_SITE_ID = "3"
CQ_BOOKING_PAGE = f"{CQ_BASE}/public/appointments/jacobsens/index.html?lang=en"
CQ_API = f"{CQ_BASE}/api/external/v4/sites/{CQ_SITE_ID}/appointments"
CQ_SERVICE_ID = 20  # "Ansøger efter EU-reglerne" (apply under EU regulations)
CQ_DAYS_AHEAD = 60  # the site clamps its own booking window to ~40 days regardless
# The site has "appointments_use_subtasks": true, so the real subtask id must be sent
# or the API silently answers with every day marked open and zero availability on all
# of them. Ids 46/47/48 all belong to service 20 and return identical availability;
# an id outside that set is the failure mode above, so do not change this blindly.
CQ_SUBTASK_ID = 46  # "Jeg vil ansøge om ophold efter EU-reglerne"
CQ_SUBTASK = {
    "subtask_items[][subtask_id]": str(CQ_SUBTASK_ID),
    "subtask_items[][number]": "1",  # one person
}


class FlowError(RuntimeError):
    """The booking flow did not yield a usable availability page."""


@dataclass(frozen=True)
class Slot:
    date: str
    time: str

    @property
    def key(self) -> str:
        return f"{self.date}|{self.time}"


def normalize(text: str) -> str:
    """Strip the superscript ordinals the site uses in dates ("September 28th" -> "September 28")."""
    stripped = SUPERSCRIPT_RE.sub("", text)
    return SPACE_BEFORE_PUNCT_RE.sub(r"\1", SPACE_RE.sub(" ", stripped)).strip()


def parse_fds_slots(html: str) -> list[Slot]:
    """Extract every bookable slot from a rendered TimeSelection page.

    A date is considered open only when it renders ``.time button`` elements,
    so the wording of the "no availability" copy cannot cause false positives.
    """
    soup = BeautifulSoup(html, "html.parser")

    if soup.select_one(".date-list") is None:
        if "FlowStateIsMissing" in html or "An error occurred" in html:
            raise FlowError("flow state missing or page returned an error")
        raise FlowError("no .date-list found; page layout may have changed")

    slots: list[Slot] = []
    for block in soup.select(".date-list .date"):
        header = block.select_one(".header-text")
        if header is None:
            continue
        date_label = normalize(header.get_text(" ", strip=True))
        if not date_label:
            continue
        for button in block.select(".times-list .time button"):
            time_label = normalize(button.get_text(" ", strip=True))
            if time_label:
                slots.append(Slot(date=date_label, time=time_label))
    return slots


def fetch_fds_slots(timeout: float) -> list[Slot]:
    """Run the FrontDeskSuite booking flow from a clean session."""
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    ) as client:
        response = client.get(FDS_START_URL)
        response.raise_for_status()
        return parse_fds_slots(response.text)


def parse_cq_slots(day: str, payload: dict) -> list[Slot]:
    """Bookable slots for one day. ``available > 0`` is the only reliable signal."""
    slots: list[Slot] = []
    for entry in payload.get("available_time_slots", []):
        if entry.get("available", 0) > 0 and entry.get("time_of_slot"):
            slots.append(Slot(date=day, time=str(entry["time_of_slot"])))
    return slots


def fetch_cq_slots(timeout: float) -> list[Slot]:
    """Query the CleverQ API for every open day in the booking window.

    Costs 2 + N requests (N = open days), so it suits a slower poll interval
    than FrontDeskSuite.
    """
    slots: list[Slot] = []
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    ) as client:
        page = client.get(CQ_BOOKING_PAGE)
        page.raise_for_status()
        csrf = CSRF_RE.search(page.text)
        if csrf is None:
            raise FlowError("no CSRF token on the CleverQ booking page")
        headers = {"X-CSRF-TOKEN": csrf.group(1), "Accept": "application/json"}

        today = date.today()
        params = {
            "service_id": CQ_SERVICE_ID,
            "from_day": today.isoformat(),
            "to_day": (today + timedelta(days=CQ_DAYS_AHEAD)).isoformat(),
            "mode_active": "true",
            **CQ_SUBTASK,
        }
        response = client.get(f"{CQ_API}/available_days", params=params, headers=headers)
        response.raise_for_status()
        days = [d["day"] for d in response.json().get("available_days", [])]
        log.debug("CleverQ reports %d open day(s)", len(days))

        for day in days:
            response = client.get(
                f"{CQ_API}/available_time_slots",
                params={
                    "service_id": CQ_SERVICE_ID,
                    "day": day,
                    "show_all": "true",
                    **CQ_SUBTASK,
                },
                headers=headers,
            )
            response.raise_for_status()
            slots.extend(parse_cq_slots(day, response.json()))

    if days and not slots:
        # An unusable subtask_id looks exactly like "fully booked": every day is
        # reported open but no slot is bookable. Worth saying out loud.
        log.warning(
            "CleverQ reported %d open day(s) but no bookable slot; if this "
            "persists, re-check CQ_SUBTASK_ID against the site's /sites/3 payload",
            len(days),
        )
    return slots


@dataclass(frozen=True)
class Site:
    key: str
    label: str
    booking_url: str
    fetcher: Callable[[float], list[Slot]]
    interval: float = DEFAULT_INTERVAL
    earliest_only: bool = False


SITES: dict[str, Site] = {
    "nyropsgade": Site(
        key="nyropsgade",
        label="SIRI Copenhagen - Nyropsgade (CPR)",
        booking_url=FDS_START_URL,
        fetcher=fetch_fds_slots,
    ),
    "carljacobsens": Site(
        key="carljacobsens",
        label="SIRI Copenhagen - Carl Jacobsens Vej (EU residence)",
        booking_url=CQ_BOOKING_PAGE,
        fetcher=fetch_cq_slots,
        interval=180.0,
        earliest_only=True,
    ),
}


class TelegramError(RuntimeError):
    """A rejected Telegram API call. `permanent` errors will never succeed on retry."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


TELEGRAM_HINTS = {
    "unauthorized": "the bot token is invalid or has been revoked; get a new one from @BotFather",
    "chat not found": (
        "the bot cannot reach that chat id. Open your bot in Telegram, press Start "
        "or send it any message, then re-read the id from getUpdates"
    ),
    "bot was blocked by the user": "unblock the bot, then it can message you again",
    "bot is not a member": "start the bot first so it can open a chat with you",
    "message text is empty": "the message body was empty",
    "message is too long": "telegram caps messages at 4096 characters",
    "not enough rights": "check the bot token belongs to the bot you intend to use",
}


def telegram_send(text: str, timeout: float) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )

    try:
        payload = response.json()
    except ValueError:
        raise TelegramError(
            f"Telegram returned HTTP {response.status_code} with a non-JSON body: "
            f"{response.text[:200]}",
            permanent=response.status_code < 500,
        ) from None

    if response.status_code == 200 and payload.get("ok"):
        return

    description = str(payload.get("description", "no description returned"))
    lowered = description.lower()
    hint = next((v for k, v in TELEGRAM_HINTS.items() if k in lowered), None)
    permanent = response.status_code < 500 and response.status_code != 429
    message = f"Telegram rejected the message: {description}"
    if hint:
        message = f"{message} ({hint})"
    raise TelegramError(message, permanent=permanent)


def redact(text: str) -> str:
    """Strip the bot token out of anything about to be logged.

    httpx exception messages embed the request URL, and the bot token is part
    of that URL, so an unredacted error message leaks the secret into logs.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return text.replace(token, "<redacted>") if token else text


def deliver(text: str, attempts: int = 3) -> bool:
    """Send a Telegram message, retrying transient failures. Returns delivery status."""
    for attempt in range(1, attempts + 1):
        try:
            telegram_send(text, NOTIFY_TIMEOUT)
            return True
        except (httpx.HTTPError, TelegramError) as exc:
            detail = redact(str(exc))
            if isinstance(exc, TelegramError) and exc.permanent:
                log.error("telegram rejected the message; not retrying: %s", detail)
                return False
            log.warning(
                "telegram send failed (attempt %d/%d): %s", attempt, attempts, detail
            )
            if attempt < attempts:
                time.sleep(2**attempt)
    log.error("telegram message not delivered; will retry on the next poll")
    return False


def format_slots(slots: list[Slot], heading: str) -> str:
    by_date: dict[str, list[str]] = {}
    for slot in slots:
        by_date.setdefault(slot.date, []).append(slot.time)

    lines = [heading]
    for day, times in by_date.items():
        lines.append(f"{day}: {', '.join(sorted(times))}")
    return "\n".join(lines)


def render_slots(site: Site, slots: list[Slot], heading: str) -> str:
    """Body of a notification about ``slots``.

    CleverQ opens hundreds of 5-minute slots at once, so listing every one of them
    is unreadable. For a site marked ``earliest_only`` the message carries just the
    first bookable date. Callers pass the full set of currently open slots there,
    so every message reports the best date on offer rather than the best date among
    the newly seen ones.
    """
    if not site.earliest_only:
        return format_slots(slots, heading)

    summary = heading.rstrip(":")
    if slots:
        summary += f" — earliest available date {min(slot.date for slot in slots)}"
    return summary


@dataclass
class Tracker:
    """Per-site polling state, persisted between runs."""

    seen: set[str] = field(default_factory=set)
    last_heartbeat: str | None = None
    first_run: bool = True
    failures: int = 0
    next_poll: float = 0.0


def state_path_for(site: Site, args: argparse.Namespace, count: int) -> Path:
    if args.state_file is not None and count == 1:
        return args.state_file
    if args.state_dir is not None:
        return args.state_dir / f"state-{site.key}.json"
    return Path(f"state-{site.key}.json")


def resolve_sites(requested: str) -> list[Site]:
    keys = [k.strip() for k in requested.split(",") if k.strip()]
    if not keys or keys == ["all"]:
        return list(SITES.values())
    unknown = [k for k in keys if k not in SITES]
    if unknown:
        raise SystemExit(
            f"unknown site(s): {', '.join(unknown)}. Choose from: {', '.join(SITES)}"
        )
    return [SITES[k] for k in keys]


def poll_site(
    site: Site,
    tracker: Tracker,
    path: Path,
    interval: float,
    heartbeat_hours: float,
    notify: bool,
) -> bool:
    """Poll one site once. Returns True on success."""
    try:
        slots = site.fetcher(DEFAULT_TIMEOUT)
    except (httpx.HTTPError, FlowError) as exc:
        tracker.failures += 1
        delay = min(interval * (2 ** min(tracker.failures, 5)), 900)
        log.error("[%s] poll failed (%s); retrying in %.0fs", site.key, exc, delay)
        tracker.next_poll = time.monotonic() + delay
        return False

    if tracker.failures:
        log.info("[%s] recovered after %d failed poll(s)", site.key, tracker.failures)
    tracker.failures = 0

    current = {slot.key for slot in slots}
    new_keys = current - tracker.seen

    baseline = tracker.first_run
    tracker.first_run = False
    delivered = True

    def send(body: str) -> bool:
        return deliver(f"{site.label}\n{body}\n\nBook here: {site.booking_url}")

    if baseline:
        log.info("[%s] baseline recorded: %d slot(s) open", site.key, len(slots))
        if notify:
            delivered = send(
                render_slots(site, slots, f"Watcher started. {len(slots)} slot(s) open:")
            )
    elif new_keys:
        new_slots = [slot for slot in slots if slot.key in new_keys]
        # Only the affected days: a single release can add hundreds of 5-minute
        # slots, and listing every one floods the logs.
        log.info(
            "[%s] %d new slot(s) on %s",
            site.key,
            len(new_slots),
            ", ".join(sorted({slot.date for slot in new_slots})),
        )
        if notify:
            # An earliest_only site reports the best date currently bookable, which
            # is not necessarily among the slots that just appeared.
            shown = slots if site.earliest_only else new_slots
            delivered = send(
                render_slots(site, shown, f"{len(new_slots)} NEW slot(s) available:")
            )

    if delivered:
        tracker.seen |= current
    save_state(
        path, {"seen": sorted(tracker.seen), "last_heartbeat": tracker.last_heartbeat}
    )

    heartbeat_due = (
        tracker.last_heartbeat is None
        or now() - datetime.fromisoformat(tracker.last_heartbeat)
        >= timedelta(hours=heartbeat_hours)
    )
    if heartbeat_due:
        if notify and not baseline:
            delivered = send(
                render_slots(site, slots, f"Status: {len(slots)} slot(s) open:")
                if slots
                else "Status: no slots open right now."
            )
        if delivered:
            tracker.last_heartbeat = now().isoformat()
        save_state(
            path,
            {"seen": sorted(tracker.seen), "last_heartbeat": tracker.last_heartbeat},
        )
        log.info("[%s] heartbeat: %d slot(s) open", site.key, len(slots))

    tracker.next_poll = time.monotonic() + interval + random.uniform(
        0, min(5.0, interval * 0.1)
    )
    return True


def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": [], "last_heartbeat": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("state file unreadable (%s); starting fresh", exc)
        return {"seen": [], "last_heartbeat": None}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def missing_credentials() -> list[str]:
    return [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        if not os.environ.get(name)
    ]


def main() -> int:
    env_interval = os.environ.get("POLL_INTERVAL")
    parser = argparse.ArgumentParser(
        description="Watch SIRI Copenhagen booking pages for new appointment slots.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="sites: " + ", ".join(f"{k} ({v.label})" for k, v in SITES.items()),
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("SITES", "all"),
        help="comma-separated site keys, or 'all' (default: %(default)s)",
    )
    parser.add_argument(
        "--once", action="store_true", help="poll each selected site once, then exit"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(env_interval) if env_interval else None,
        help="override every site's poll interval, in seconds",
    )
    parser.add_argument(
        "--heartbeat-hours",
        type=float,
        default=float(os.environ.get("HEARTBEAT_HOURS", DEFAULT_HEARTBEAT_HOURS)),
        help="hours between status pings (default: %(default)s)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ["STATE_DIR"]) if os.environ.get("STATE_DIR") else None,
        help="directory for per-site state files",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="explicit state file; only valid when watching a single site",
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=float(os.environ.get("MAX_RUNTIME", 0)),
        help="stop after this many minutes and save state (0 = run forever)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    sites = resolve_sites(args.site)
    if args.state_file is not None and len(sites) > 1:
        log.error("--state-file cannot be used with multiple sites; use --state-dir")
        return 2

    absent = missing_credentials()
    if absent and not args.once:
        log.error(
            "missing required environment variables: %s\n"
            "Copy .env.example to .env and fill them in (see README).",
            ", ".join(absent),
        )
        return 2
    notify = not absent

    trackers: dict[str, Tracker] = {}
    for site in sites:
        path = state_path_for(site, args, len(sites))
        saved = load_state(path)
        heartbeat = saved["last_heartbeat"]
        trackers[site.key] = Tracker(
            seen=set(saved["seen"]),
            last_heartbeat=heartbeat,
            first_run=not saved["seen"] and heartbeat is None,
            next_poll=0.0,
        )

    intervals = {
        site.key: (args.interval if args.interval is not None else site.interval)
        for site in sites
    }
    started = time.monotonic()

    while True:
        ok = True
        for site in sites:
            tracker = trackers[site.key]
            if args.once or tracker.next_poll <= time.monotonic():
                ok &= poll_site(
                    site,
                    tracker,
                    state_path_for(site, args, len(sites)),
                    intervals[site.key],
                    args.heartbeat_hours,
                    notify,
                )

        if args.once:
            return 0 if ok else 1

        if args.max_runtime and time.monotonic() - started >= args.max_runtime * 60:
            log.info("max runtime of %g min reached; state saved", args.max_runtime)
            return 0

        waits = [
            max(0.0, trackers[site.key].next_poll - time.monotonic()) for site in sites
        ]
        time.sleep(min(waits) if waits else args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
