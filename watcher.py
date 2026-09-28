#!/usr/bin/env python3
"""Watch the SIRI Copenhagen booking page and alert on newly released appointment slots.

The reservation site is an ASP.NET MVC flow whose TimeSelection page is only
rendered when the request carries a server-side ``ReserveTimeState`` cookie. The
page cannot be fetched directly; it must be reached by starting the flow, so
every poll replays ``StartReservation`` from a clean cookie jar.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("watcher")

BASE_URL = "https://reservation.frontdesksuite.com"
SITE = "kk/SIRI%20Copenhagen"
PAGE_ID = "2dcd2a7a-e666-4cf2-86d2-e035a8a638ee"
BUTTON_ID = "a4b27a7d-15e6-4420-bc1e-176f58fe5f92"

START_URL = (
    f"{BASE_URL}/{SITE}/ReserveTime/StartReservation"
    f"?pageId={PAGE_ID}&buttonId={BUTTON_ID}&culture=en&uiCulture=en"
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SUPERSCRIPT_RE = re.compile(r"[\u00b2\u00b3\u00b9\u02b0-\u02ff\u1d2c-\u1dbf\u2070-\u209f]")
SPACE_RE = re.compile(r"\s+")
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:])")

DEFAULT_INTERVAL = 60.0
DEFAULT_HEARTBEAT_HOURS = 24.0
DEFAULT_TIMEOUT = 30.0
NOTIFY_TIMEOUT = 15.0


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


def parse_slots(html: str) -> list[Slot]:
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
        date = normalize(header.get_text(" ", strip=True))
        if not date:
            continue
        for button in block.select(".times-list .time button"):
            time_label = normalize(button.get_text(" ", strip=True))
            if time_label:
                slots.append(Slot(date=date, time=time_label))
    return slots


def fetch_slots(timeout: float) -> list[Slot]:
    """Run the booking flow from a clean session and return the current slots."""
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    ) as client:
        response = client.get(START_URL)
        response.raise_for_status()
        return parse_slots(response.text)


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
    for date, times in by_date.items():
        lines.append(f"{date}: {', '.join(sorted(times))}")
    return "\n".join(lines)


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="poll a single time, then exit"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("POLL_INTERVAL", DEFAULT_INTERVAL)),
        help="seconds between polls (default: %(default)s)",
    )
    parser.add_argument(
        "--heartbeat-hours",
        type=float,
        default=float(os.environ.get("HEARTBEAT_HOURS", DEFAULT_HEARTBEAT_HOURS)),
        help="hours between status pings (default: %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("STATE_FILE", "state.json")),
        help="where to persist seen slots (default: %(default)s)",
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

    absent = missing_credentials()
    if absent and not args.once:
        log.error(
            "missing required environment variables: %s\n"
            "Copy .env.example to .env and fill them in (see README).",
            ", ".join(absent),
        )
        return 2

    notify = not absent
    state = load_state(args.state_file)
    seen: set[str] = set(state["seen"])
    last_heartbeat = state["last_heartbeat"]
    failures = 0
    first_run = not seen and last_heartbeat is None
    started = time.monotonic()

    while True:
        try:
            slots = fetch_slots(DEFAULT_TIMEOUT)
        except (httpx.HTTPError, FlowError) as exc:
            failures += 1
            delay = min(args.interval * (2**min(failures, 5)), 900)
            log.error("poll failed (%s); retrying in %.0fs", exc, delay)
            if args.once:
                return 1
            time.sleep(delay)
            continue

        if failures:
            log.info("recovered after %d failed poll(s)", failures)
        failures = 0

        current = {slot.key for slot in slots}
        new_keys = current - seen

        baseline = first_run
        first_run = False

        delivered = True

        if baseline:
            log.info("baseline recorded: %d slot(s) currently open", len(slots))
            if notify:
                delivered = deliver(
                    format_slots(
                        slots, f"Watcher started. {len(slots)} slot(s) open right now:"
                    )
                    + f"\n\nBook here: {START_URL}"
                )
        elif new_keys:
            new_slots = [slot for slot in slots if slot.key in new_keys]
            log.info("%d new slot(s): %s", len(new_slots), sorted(new_keys))
            if notify:
                delivered = deliver(
                    format_slots(
                        new_slots, f"{len(new_slots)} NEW slot(s) available:"
                    )
                    + f"\n\nBook here: {START_URL}"
                )

        if delivered:
            seen |= current
        save_state(args.state_file, {"seen": sorted(seen), "last_heartbeat": last_heartbeat})

        heartbeat_due = (
            last_heartbeat is None
            or now() - datetime.fromisoformat(last_heartbeat)
            >= timedelta(hours=args.heartbeat_hours)
        )
        if heartbeat_due:
            if notify and not baseline:
                delivered = deliver(
                    format_slots(slots, f"Status: {len(slots)} slot(s) open right now:")
                    if slots
                    else "Status: no slots open right now."
                )
            if delivered:
                last_heartbeat = now().isoformat()
            save_state(
                args.state_file,
                {"seen": sorted(seen), "last_heartbeat": last_heartbeat},
            )
            log.info("heartbeat: %d slot(s) open", len(slots))

        if args.once:
            return 0

        if args.max_runtime and time.monotonic() - started >= args.max_runtime * 60:
            log.info("max runtime of %g min reached; state saved", args.max_runtime)
            return 0

        time.sleep(args.interval + random.uniform(0, min(5.0, args.interval * 0.1)))


if __name__ == "__main__":
    raise SystemExit(main())
