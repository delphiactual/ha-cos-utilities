"""Turn a raw capture into fixtures that are safe to commit (dev only, not shipped).

    python scripts/scrub_fixtures.py fixtures_raw/<capture> [--out tests/fixtures]

Reads a capture made by scripts/record_fixtures.py and writes only the trimmed set
of windows the tests need to tests/fixtures/, with every account identifier, token,
name and address swapped for a stable fake:

- Usage windows are copied byte for byte except for the exact original values
  (customer id, meter number, service number, service id, contract number, account
  context, ...), which are swapped in place. Nothing else is touched.
- The login responses (token, account list, services) have the same values swapped
  by key, plus any value under a name, address, email, phone or token key.

Afterwards every written file is searched for every original value and for known
secrets. Any hit deletes the output and exits non-zero, naming the file and the kind
of value but never printing the value itself.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "fixtures"
TZ = ZoneInfo("America/Denver")

TOKEN_PATH = "/rest/oauth/token"
ACCOUNTS_PATH = "/rest/account/list/"
SERVICES_PATH = "/rest/account/services/"
LOGIN_PATHS = {
    TOKEN_PATH: "token",
    ACCOUNTS_PATH: "accounts",
    SERVICES_PATH: "services",
}
HOURLY_PATH = "/rest/usage/detail/day"
DAILY_PATH = "/rest/usage/detail/month"
USAGE_PATHS = (HOURLY_PATH, DAILY_PATH)

# Values under keys like these are swapped in the login files whatever they hold.
SENSITIVE_KEY = re.compile(
    r"name|address|street|city|state|zip|postal|email|mail|phone|contact|premise|"
    r"location|latitude|longitude|owner|token|secret|password|account|customer|"
    r"contract|meter|service|mfa|meas|jti|transaction|lastfailed",
    re.IGNORECASE,
)
# Kept as they are: not identifying, and the client reads them.
KEEP_KEYS = {"token_type", "expires_in", "expires", "scope"}
SERVICE_KEYS = (
    "meterNumber",
    "serviceNumber",
    "serviceId",
    "serviceContract",
    "contractAccountNumber",
)
PREFIX_ALIASES = {"E-": "elec", "G-": "gas", "W-": "water"}
SERVICE_PREFIXES = {"elec": "E-", "gas": "G-", "water": "W-"}
# Anything shaped like an identifier is swapped whatever its key is called.
ID_LIKE = re.compile(
    r"^(\d{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
MIN_STRING = 4  # shorter values only get swapped by key, never searched for
MIN_DIGITS = 6


class LeakError(Exception):
    """A value that should have been swapped is still in the output."""


class Fakes:
    """Original value -> stable fake, the same everywhere it appears."""

    def __init__(self) -> None:
        self.map: dict = {}
        self._counts: dict[str, int] = defaultdict(int)

    def set(self, original, fake) -> None:
        self.map.setdefault(original, fake)

    def get(self, original, role: str):
        if original not in self.map:
            self._counts[role] += 1
            if isinstance(original, (int, float)):
                self.map[original] = 900_000_000 + len(self.map) + 1
            else:
                self.map[original] = f"fake-{role}-{self._counts[role]}"
        return self.map[original]


def significant(value) -> bool:
    """Long enough to be an identifier, so safe to swap wherever it appears."""
    if isinstance(value, str):
        return len(value) >= MIN_STRING
    return len(str(int(value))) >= MIN_DIGITS


def is_value(node) -> bool:
    """A leaf worth swapping: a non-empty string or a number (not a bool)."""
    if isinstance(node, bool):
        return False
    return isinstance(node, (int, float)) or (isinstance(node, str) and node != "")


def leaves(node, key=""):
    """Every (key, value) leaf, where key is the name of the nearest dict key."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from leaves(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from leaves(v, key)
    elif is_value(node):
        yield key, node


def map_leaves(node, fn, key=""):
    if isinstance(node, dict):
        return {k: map_leaves(v, fn, k) for k, v in node.items()}
    if isinstance(node, list):
        return [map_leaves(v, fn, key) for v in node]
    return fn(key, node) if is_value(node) else node


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or "value"


def alias_of_service(service_number) -> str:
    return PREFIX_ALIASES.get(str(service_number)[:2], "other")


def collect_identities(raw: dict[str, object]) -> Fakes:
    """Find the originals in the login responses and give each its fake."""
    fakes = Fakes()
    token = raw.get("token")
    if isinstance(token, dict):
        customer = (token.get("user") or {}).get("customerId")
        if is_value(customer):
            fakes.set(customer, "fake-customer-1")
    accounts = raw.get("accounts")
    if isinstance(accounts, dict):
        context = (accounts.get("account") or [{}])[0]
        for key, value in leaves(context):
            if significant(value) or SENSITIVE_KEY.search(key):
                fakes.get(value, f"context-{slug(key)}")
    services = raw.get("services")
    if isinstance(services, dict):
        listed = (services.get("accountSummaryType") or {}).get(
            "servicesForGraph"
        ) or []
        for service in listed:
            alias = alias_of_service(service.get("serviceNumber"))
            prefix = SERVICE_PREFIXES.get(alias, "X-")
            roles = {
                "meterNumber": f"fake-meter-{alias}",
                "serviceNumber": f"{prefix}FAKE",
                "serviceId": f"fake-service-{alias}",
                "serviceContract": f"fake-contract-{alias}",
                "contractAccountNumber": "fake-account-1",
            }
            for key in SERVICE_KEYS:
                value = service.get(key)
                if not is_value(value):
                    continue
                fake = roles[key]
                if isinstance(value, (int, float)):
                    fake = fakes.get(value, f"{alias}-{slug(key)}")
                fakes.set(value, fake)
    return fakes


def swap_inside(value: str, fakes: Fakes) -> str:
    """Swap known originals that sit inside a longer text, such as a description."""
    ordered = sorted(fakes.map.items(), key=lambda kv: -len(str(kv[0])))
    for original, fake in ordered:
        if isinstance(original, str) and len(original) >= MIN_STRING:
            value = re.sub(re.escape(original), str(fake), value, flags=re.IGNORECASE)
    return value


def scrub_login(name: str, parsed, fakes: Fakes, kept: dict[str, set]):
    """Swap values by key and by known original; remember the keys left as they are.

    Runs twice so every original is known before any text is searched for them,
    whatever order the fields come in.
    """

    def swap(key, value):
        if value in fakes.map and (significant(value) or SENSITIVE_KEY.search(key)):
            return fakes.map[value]
        if key in KEEP_KEYS:
            kept[name].add(key)
            return value
        if SENSITIVE_KEY.search(key) or (
            isinstance(value, str) and ID_LIKE.match(value)
        ):
            return fakes.get(value, slug(key))
        kept[name].add(key)
        return swap_inside(value, fakes) if isinstance(value, str) else value

    map_leaves(parsed, swap)
    return map_leaves(parsed, swap)


def checkable(fakes: Fakes, extra: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(strings, digit strings) that must not appear anywhere in the output."""
    strings = [o for o in fakes.map if isinstance(o, str) and len(o) >= MIN_STRING]
    strings += [s for s in extra if len(s) >= MIN_STRING]
    numbers = [
        str(o).rstrip("0").rstrip(".") if isinstance(o, float) else str(o)
        for o in fakes.map
        if isinstance(o, (int, float)) and len(str(int(o))) >= MIN_DIGITS
    ]
    return strings, numbers


def swap_bytes(raw: bytes, fakes: Fakes) -> tuple[bytes, int]:
    """Replace the exact original values in a usage window, leaving the rest alone."""
    text = raw.decode("utf-8")
    swapped = 0
    ordered = sorted(fakes.map.items(), key=lambda kv: -len(str(kv[0])))
    for original, fake in ordered:
        if isinstance(original, str):
            if len(original) < MIN_STRING:
                continue
            if len(original) >= 6:
                count = text.count(original)
                text = text.replace(original, str(fake))
            else:
                count = text.count(f'"{original}"')
                text = text.replace(f'"{original}"', f'"{fake}"')
        else:
            digits = str(int(original))
            if len(digits) < MIN_DIGITS:
                continue
            pattern = re.compile(rf"(?<![\d.]){re.escape(digits)}(?![\d.])")
            text, count = pattern.subn(str(fake), text)
        swapped += count
    return text.encode("utf-8"), swapped


def leak_check(files: dict[str, bytes], fakes: Fakes, extra: tuple[str, ...]) -> list:
    """(file, kind) for every original or secret that is still in the output."""
    strings, numbers = checkable(fakes, extra)
    problems = []
    for name, data in files.items():
        text = data.decode("utf-8")
        lowered = text.lower()
        for index, value in enumerate(strings):
            if value.lower() in lowered:
                problems.append((name, f"original string #{index}"))
        for index, digits in enumerate(numbers):
            if re.search(rf"(?<![\d.]){re.escape(digits)}(?![\d.])", text):
                problems.append((name, f"original number #{index}"))
        for marker in ("bearer ", "authorization"):
            if marker in lowered:
                problems.append((name, f"credential marker {marker.strip()!r}"))
    return problems


def window_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def dst_dates(captured: date) -> list[date]:
    """The most recent spring and fall clock changes before the capture."""
    found: dict[str, date] = {}
    day = captured
    for _ in range(800):
        before = datetime(day.year, day.month, day.day, tzinfo=TZ).utcoffset()
        after_day = day + timedelta(days=1)
        after = datetime(
            after_day.year, after_day.month, after_day.day, tzinfo=TZ
        ).utcoffset()
        if before != after:
            found.setdefault("spring" if after > before else "fall", day)
        if len(found) == 2:
            break
        day -= timedelta(days=1)
    return sorted(found.values())


def load_history(path: Path) -> list[dict]:
    try:
        history = json.loads(path.read_bytes()).get("history")
    except (ValueError, AttributeError):
        return []
    return history if isinstance(history, list) else []


def select_windows(capture: Path, entries: list[dict], captured: date):
    """entry n -> set of reasons it is kept; also notes on how each was chosen."""
    usage = [
        e
        for e in entries
        if e["url_path"] in USAGE_PATHS and e["alias"] in ("elec", "gas")
    ]
    chosen: dict[int, set[str]] = defaultdict(set)
    notes: list[str] = []
    hourly = [e for e in usage if e["url_path"] == HOURLY_PATH and e["status"] == 200]

    for e in usage:
        span = window_date(e["toDate"]) - window_date(e["fromDate"])
        to = window_date(e["toDate"])
        if to >= captured or (
            span <= timedelta(days=1) and to >= captured - timedelta(days=3)
        ):
            chosen[e["n"]].add("recent")
    for change in dst_dates(captured):
        for e in usage:
            if window_date(e["fromDate"]) <= change <= window_date(e["toDate"]):
                chosen[e["n"]].add(f"dst-{change.isoformat()}")
    # Null export: a week where every export value is null, else the nullest.
    best = None
    for e in hourly:
        if e["alias"] != "elec" or not e["rows"]:
            continue
        rows = load_history(capture / e["file"])
        values = [r for r in rows if r.get("usageConsumptionValue") is not None]
        if not values:
            continue
        nulls = sum(1 for r in values if r.get("netReceivedValue") in (None, ""))
        key = (nulls == len(values), nulls / len(values), window_date(e["toDate"]))
        if best is None or key > best[0]:
            best = (key, e)
    if best:
        chosen[best[1]["n"]].add("null-export")
        notes.append(
            "null export week: "
            + ("every value null" if best[0][0] else f"{best[0][1]:.0%} of values null")
        )
    # Empty and service-start windows, per meter.
    for alias in ("elec", "gas"):
        mine = [e for e in hourly if e["alias"] == alias]
        empties = [e for e in mine if e["rows"] == 0]
        if empties:
            chosen[empties[0]["n"]].add("empty")
        with_values = []
        for e in mine:
            rows = load_history(capture / e["file"])
            if any(r.get("usageConsumptionValue") is not None for r in rows):
                with_values.append(e)
        if with_values:
            first = min(with_values, key=lambda e: window_date(e["toDate"]))
            chosen[first["n"]].add("service-start")
            notes.append(f"{alias} first read window ends {first['toDate'][:10]}")
            before = [
                e
                for e in mine
                if e["rows"]
                and window_date(e["toDate"]) <= window_date(first["fromDate"])
            ]
            if before:
                nearest = max(before, key=lambda e: window_date(e["toDate"]))
                chosen[nearest["n"]].add("before-service-start")
        errors = [e for e in usage if e["alias"] == alias and e["status"] != 200]
        if errors:
            chosen[errors[0]["n"]].add("error")
    return chosen, notes


def scrub(capture: Path, out: Path, forbidden: tuple[str, ...] = ()) -> dict:
    """Write the trimmed, scrubbed fixtures. Raises LeakError (and writes nothing)."""
    index = json.loads((capture / "capture.json").read_text(encoding="utf-8"))
    entries = index["entries"]
    captured_text = index.get("captured_at_denver") or index["captured_at_utc"]
    captured_dt = datetime.fromisoformat(captured_text).astimezone(TZ)
    by_path: dict[str, dict] = {}
    for e in entries:
        if e["url_path"] in LOGIN_PATHS and e["status"] == 200:
            by_path.setdefault(e["url_path"], e)
    raw_login = {
        LOGIN_PATHS[p]: json.loads((capture / e["file"]).read_bytes())
        for p, e in by_path.items()
    }
    fakes = collect_identities(raw_login)

    kept: dict[str, set] = defaultdict(set)
    files: dict[str, bytes] = {}
    login_files = {}
    for name, parsed in raw_login.items():
        text = json.dumps(scrub_login(name, parsed, fakes, kept), separators=(",", ":"))
        filename = f"login_{name}.json"
        files[filename] = text.encode("utf-8")
        login_files[name] = filename

    chosen, notes = select_windows(capture, entries, captured_dt.date())
    by_n = {e["n"]: e for e in entries}
    windows = []
    swapped_total = 0
    for n in sorted(chosen):
        e = by_n[n]
        data, swapped = swap_bytes((capture / e["file"]).read_bytes(), fakes)
        swapped_total += swapped
        endpoint = "hourly" if e["url_path"] == HOURLY_PATH else "daily"
        filename = (
            f"{e['alias']}_{endpoint}_{e['fromDate'][:10]}_{e['toDate'][:10]}.json.gz"
        )
        if filename in files:
            continue  # the same window recorded twice
        files[filename] = data
        windows.append(
            {
                "url_path": e["url_path"],
                "meter": e["alias"],
                "fromDate": e["fromDate"],
                "toDate": e["toDate"],
                "status": e["status"],
                "rows": e["rows"],
                "tags": sorted(chosen[n]),
                "file": filename,
            }
        )
    out_index = {
        "captured_at_denver": captured_dt.isoformat(),
        "login": login_files,
        "windows": windows,
    }
    files["index.json"] = json.dumps(out_index, indent=2).encode("utf-8")

    extra = tuple(s for s in forbidden if s)
    problems = leak_check(files, fakes, extra)
    if problems:
        raise LeakError(problems)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for name, data in files.items():
        if name.endswith(".gz"):
            # Windows compress about 10 to 1; a fixed mtime keeps the bytes stable.
            data = gzip.compress(data, mtime=0)
        (out / name).write_bytes(data)
    return {
        "files": {name: len(data) for name, data in files.items()},
        "windows": windows,
        "notes": notes,
        "swapped_in_usage_windows": swapped_total,
        "values_registered": len(fakes.map),
        "login_keys_kept": {k: sorted(v) for k, v in kept.items()},
    }


def forbidden_from_env() -> tuple[str, ...]:
    username = os.environ.get("CSU_USERNAME", "")
    return tuple(s for s in (username, username.split("@")[0]) if s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        report = scrub(args.capture, args.out, forbidden_from_env())
    except LeakError as err:
        print("LEAK CHECK FAILED; nothing was written:")
        for name, kind in err.args[0]:
            print(f"  {name}: {kind}")
        return 2
    total = sum(report["files"].values())
    print(f"Wrote {len(report['files'])} files, {total / 1024:.0f} KiB, to {args.out}")
    for w in report["windows"]:
        print(
            f"  {w['meter']:<5}{w['url_path'].rsplit('/', 1)[1]:<6}"
            f"{w['fromDate'][:10]}..{w['toDate'][:10]}  rows={w['rows']!s:<6}"
            f"{','.join(w['tags'])}"
        )
    for note in report["notes"]:
        print(f"  note: {note}")
    print(
        f"{report['values_registered']} original values swapped for fakes "
        f"({report['swapped_in_usage_windows']} places inside usage windows)."
    )
    for name, keys in report["login_keys_kept"].items():
        print(f"  login_{name}.json keys left as recorded: {', '.join(keys) or '-'}")
    print("Leak check passed. Review the login files before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
