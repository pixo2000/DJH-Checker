#!/usr/bin/env python3
"""DJH-Checker: prüft regelmäßig, ob eine Jugendherberge im gewünschten Zeitraum
verfügbar ist, und benachrichtigt via ntfy.

Konfiguration ausschließlich über Umgebungsvariablen (siehe README.md / .env.example).
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

BASE_URL = "https://www.jugendherberge.de"
SEARCH_URL = f"{BASE_URL}/angebote/suche/"
AUTOCOMPLETE_URL = f"{BASE_URL}/api/autoComplete"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 DJH-Checker"
)

PERSON_CATEGORIES = {
    "PERSONS_16": "SixteenYears",
    "PERSONS_17": "SeventeenYears",
    "PERSONS_18_26": "EighteenToTwentySixYears",
    "PERSONS_27_PLUS": "TwentySevenYearsOrOlder",
}

log = logging.getLogger("djh-checker")


@dataclass
class Config:
    hostel_name: str
    hostel_id: str | None
    arrival: str
    departure: str
    persons: dict[str, int]
    interval: int
    data_dir: Path
    ntfy_url: str
    ntfy_topic: str | None
    ntfy_token: str | None
    ntfy_priority: str
    notify_unavailable: bool
    notify_startup: bool
    run_once: bool
    timeout: int
    error_notify_after: int
    anomaly_notify_after: int

    @property
    def total_persons(self) -> int:
        return sum(self.persons.values())


@dataclass
class Offer:
    title: str
    price: str
    bookable: bool


@dataclass
class Result:
    hostel_id: str
    hostel_name: str
    found: bool
    available: bool
    offers: list[Offer] = field(default_factory=list)

    @property
    def bookable_offers(self) -> list[Offer]:
        return [o for o in self.offers if o.bookable]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _parse_date(value: str, name: str) -> str:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    raise ValueError(f"{name}={value!r} ist kein gültiges Datum (TT.MM.JJJJ oder JJJJ-MM-TT)")


def _parse_bool(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "ja", "on")


def load_config() -> Config:
    name = _env("DJH_NAME")
    hostel_id = _env("DJH_ID")
    if not name and not hostel_id:
        raise ValueError("DJH_NAME oder DJH_ID muss gesetzt sein")

    arrival = _env("ARRIVAL")
    departure = _env("DEPARTURE")
    if not arrival or not departure:
        raise ValueError("ARRIVAL und DEPARTURE müssen gesetzt sein")
    arrival = _parse_date(arrival, "ARRIVAL")
    departure = _parse_date(departure, "DEPARTURE")
    if datetime.strptime(departure, "%d.%m.%Y") <= datetime.strptime(arrival, "%d.%m.%Y"):
        raise ValueError("DEPARTURE muss nach ARRIVAL liegen")

    persons = {key: int(_env(key, "0")) for key in PERSON_CATEGORIES}
    if any(v < 0 for v in persons.values()):
        raise ValueError("Personenanzahl darf nicht negativ sein")
    if sum(persons.values()) == 0:
        persons["PERSONS_18_26"] = int(_env("PERSONS", "1"))
    if sum(persons.values()) <= 0:
        raise ValueError("Mindestens eine Person angeben")

    interval = int(_env("CHECK_INTERVAL", "1800"))
    if interval < 60:
        raise ValueError("CHECK_INTERVAL muss mindestens 60 Sekunden sein")

    return Config(
        hostel_name=name or "",
        hostel_id=hostel_id,
        arrival=arrival,
        departure=departure,
        persons=persons,
        interval=interval,
        data_dir=Path(_env("DATA_DIR", "./data")),
        ntfy_url=_env("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=_env("NTFY_TOPIC"),
        ntfy_token=_env("NTFY_TOKEN"),
        ntfy_priority=_env("NTFY_PRIORITY", "high"),
        notify_unavailable=_parse_bool(_env("NOTIFY_UNAVAILABLE", "false")),
        notify_startup=_parse_bool(_env("NOTIFY_STARTUP", "false")),
        run_once=_parse_bool(_env("RUN_ONCE", "false")),
        timeout=int(_env("HTTP_TIMEOUT", "60")),
        error_notify_after=max(1, int(_env("ERROR_NOTIFY_AFTER", "2"))),
        anomaly_notify_after=max(1, int(_env("ANOMALY_NOTIFY_AFTER", "1"))),
    )


class CheckError(Exception):
    """Fehlgeschlagene Prüfung.

    kind: "netzwerk" (Timeout, Verbindung, 5xx), "blockiert" (403/429/Captcha)
    oder "schema" (Antwort entspricht nicht dem erwarteten Aufbau).
    """

    def __init__(self, kind: str, message: str, body: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.body = body
        self.url: str | None = None


_BLOCK_MARKERS = ("captcha", "cf-challenge", "challenge-platform", "access denied",
                  "zugriff verweigert", "too many requests", "request blocked")


def http_get(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = None
        kind = "blockiert" if exc.code in (401, 403, 429) else "netzwerk" if exc.code >= 500 else "schema"
        raise CheckError(kind, f"HTTP {exc.code} {exc.reason}", body) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CheckError("netzwerk", f"Verbindungsfehler: {reason}") from exc


def resolve_hostel_id(name: str, timeout: int) -> tuple[str, str]:
    """Löst einen Herbergsnamen über die Autocomplete-API in (ID, Name) auf."""
    url = AUTOCOMPLETE_URL + "?" + urlencode(
        {"LanguageCode": "de", "Type": "1", "SearchTerm": name}
    )
    body = http_get(url, timeout)
    try:
        data = json.loads(body)
        suggestions = data["Suggestions"]
        if not isinstance(suggestions, list):
            raise TypeError("Suggestions ist keine Liste")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        kind = "blockiert" if any(m in body.lower() for m in _BLOCK_MARKERS) else "schema"
        raise CheckError(kind, f"Autocomplete-API liefert unerwartete Antwort ({exc})", body) from exc
    hostels = [s for s in suggestions if isinstance(s, dict) and s.get("type") == 1 and "ids" in s and "value" in s]
    if not hostels:
        raise LookupError(f"Keine Jugendherberge zu {name!r} gefunden")
    for s in hostels:
        if s["value"].casefold() == name.casefold():
            return s["ids"], s["value"]
    names = ", ".join(s["value"] for s in hostels)
    if len(hostels) == 1:
        return hostels[0]["ids"], hostels[0]["value"]
    raise LookupError(f"{name!r} ist nicht eindeutig: {names} – bitte genauer angeben oder DJH_ID setzen")


def build_search_url(cfg: Config, hostel_id: str, hostel_name: str) -> str:
    params: dict[str, object] = {
        "searchTerm": hostel_name,
        "PrimaryActiveResultTab": "Accommodation",
        "Active": "true",
        "SearchTerm": hostel_name,
        "SelectedAutoCompleteEntryIds": hostel_id,
        "SelectedAutoCompleteEntryType": 1,
        "Travelers.Count": cfg.total_persons,
        "TravelPeriodType": 0,
        "Arrival": cfg.arrival,
        "Departure": cfg.departure,
    }
    for key, category in PERSON_CATEGORIES.items():
        if cfg.persons[key]:
            params[f"Travelers.IndividualOrFriendsTravelers.{category}"] = cfg.persons[key]
    return SEARCH_URL + "?" + urlencode(params)


_CARD_RE = re.compile(r'<div id="idAccommodation(\d+)"')
_TITLE_RE = re.compile(r'class="\[ c-teaser__title \]"[^>]*>([^<]+)<')
_RESULTS_RE = re.compile(r"<search-results data-prop-object='([^']*)'")
_TAG_RE = re.compile(r"<[^>]+>")


def parse_results(page: str, hostel_id: str, hostel_name: str) -> Result:
    """Sucht die Ergebniskarte der Herberge und liest die Angebote aus.

    Wirft CheckError, wenn die Seite nicht dem erwarteten Aufbau entspricht.
    """
    matches = list(_CARD_RE.finditer(page))
    if not matches:
        lower = page.lower()
        if any(m in lower for m in _BLOCK_MARKERS):
            raise CheckError("blockiert", "Antwort sieht nach Bot-Schutz/Captcha aus", page)
        if "<search-results" not in page and "searchresult" not in lower:
            raise CheckError("schema", f"Keine Suchergebnisse in der Antwort ({len(page)} Zeichen)", page)
        raise CheckError("schema", "Suchergebnisse ohne Herbergs-Karten", page)

    for i, m in enumerate(matches):
        if m.group(1) != str(hostel_id):
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        card = page[m.start():end]
        head = card[: card.find(">") + 1]
        title_m = _TITLE_RE.search(card)
        name = html.unescape(title_m.group(1)).strip() if title_m else hostel_name
        disabled = "searchResult--disable" in head

        blobs = _RESULTS_RE.findall(card)
        if not disabled and not blobs:
            raise CheckError("schema", f"Karte von {name} ist aktiv, enthält aber keine Angebotsdaten", page)

        offers: list[Offer] = []
        for blob in blobs:
            try:
                data = json.loads(html.unescape(blob))
            except json.JSONDecodeError as exc:
                raise CheckError("schema", f"Angebotsdaten sind kein gültiges JSON ({exc})", page) from exc
            articles = data.get("Articles") if isinstance(data, dict) else None
            if not isinstance(articles, list):
                raise CheckError("schema", "Angebotsdaten ohne 'Articles'-Liste", page)
            for art in articles:
                if not isinstance(art, dict) or "Bookable" not in art:
                    raise CheckError("schema", "Angebot ohne 'Bookable'-Feld", page)
                title = html.unescape(_TAG_RE.sub("", art.get("TitleRawHtml") or "")).strip()
                offers.append(Offer(title or "?", art.get("TotalPrice") or "", bool(art.get("Bookable"))))

        if not disabled and not offers:
            raise CheckError("schema", f"Karte von {name} ist aktiv, aber ohne Angebote", page)

        available = not disabled and any(o.bookable for o in offers)
        return Result(str(hostel_id), name, True, available, offers)

    raise CheckError(
        "schema",
        f"Herberge {hostel_name or hostel_id} (ID {hostel_id}) fehlt in den Suchergebnissen "
        f"({len(matches)} andere Karten gefunden)",
        page,
    )


def send_ntfy(cfg: Config, title: str, message: str, click: str | None = None,
              priority: str | None = None, tags: str = "") -> None:
    if not cfg.ntfy_topic:
        log.info("NTFY_TOPIC nicht gesetzt – keine Benachrichtigung gesendet")
        return
    headers = {
        # HTTP-Header müssen latin-1 sein; ntfy unterstützt RFC 2047 für UTF-8-Titel.
        "Title": _rfc2047(title),
        "Priority": priority or cfg.ntfy_priority,
        "User-Agent": "DJH-Checker",
    }
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    req = urllib.request.Request(
        f"{cfg.ntfy_url}/{cfg.ntfy_topic}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
        log.info("ntfy-Benachrichtigung gesendet (HTTP %s)", resp.status)


def _rfc2047(text: str) -> str:
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        import base64
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check(cfg: Config, hostel_id: str, hostel_name: str) -> tuple[Result, str]:
    url = build_search_url(cfg, hostel_id, hostel_name)
    log.debug("GET %s", url)
    try:
        page = http_get(url, cfg.timeout)
    except CheckError as exc:
        exc.url = url
        raise
    out = cfg.data_dir / "last_response.html"
    out.write_text(page, encoding="utf-8")
    log.debug("Antwort (%d Zeichen) gespeichert in %s", len(page), out)
    try:
        return parse_results(page, hostel_id, hostel_name), url
    except CheckError as exc:
        exc.url = url
        raise


def describe(result: Result) -> str:
    if not result.available:
        return "NICHT verfügbar"
    offers = ", ".join(f"{o.title} ({o.price})" for o in result.bookable_offers)
    return f"VERFÜGBAR – {offers}"


_ERROR_TITLES = {
    "netzwerk": "DJH-Checker: Seite nicht erreichbar",
    "blockiert": "DJH-Checker: Zugriff blockiert?",
    "schema": "DJH-Checker: Unerwartete Antwort",
}


def handle_error(cfg: Config, state: dict, exc: CheckError) -> None:
    """Zählt aufeinanderfolgende Fehler und benachrichtigt einmal pro Fehlerserie."""
    state["error_count"] = state.get("error_count", 0) + 1
    state["last_error"] = f"[{exc.kind}] {exc}"
    state["last_error_at"] = datetime.now().isoformat(timespec="seconds")
    count = state["error_count"]
    log.error("Prüfung fehlgeschlagen (%s, %d. Fehler in Folge): %s", exc.kind, count, exc)

    if exc.body:
        err_file = cfg.data_dir / "error_response.html"
        err_file.write_text(exc.body, encoding="utf-8")
        log.info("Fehlerhafte Antwort gespeichert in %s", err_file)

    threshold = cfg.error_notify_after if exc.kind == "netzwerk" else cfg.anomaly_notify_after
    if count >= threshold and not state.get("error_notified"):
        hints = {
            "netzwerk": "Timeouts/Verbindungsfehler oder Serverfehler.",
            "blockiert": "Möglicherweise IP-Sperre, Rate-Limit oder Captcha.",
            "schema": "Die Seite hat sich evtl. geändert – Checker muss ggf. angepasst werden.",
        }
        send_ntfy(
            cfg,
            _ERROR_TITLES.get(exc.kind, "DJH-Checker: Fehler"),
            f"{exc}\n{hints.get(exc.kind, '')}\n{count} Fehler in Folge – weitere Prüfungen laufen weiter.",
            click=exc.url,
            priority="high",
            tags="warning",
        )
        state["error_notified"] = True


def handle_recovery(cfg: Config, state: dict) -> None:
    if state.get("error_notified"):
        send_ntfy(cfg, "DJH-Checker funktioniert wieder",
                  f"Nach {state.get('error_count', 0)} Fehler(n) klappt die Prüfung wieder.\n"
                  f"Letzter Fehler: {state.get('last_error', '?')}",
                  priority="low", tags="white_check_mark")
    elif state.get("error_count"):
        log.info("Prüfung klappt wieder nach %d Fehler(n)", state["error_count"])
    state["error_count"] = 0
    state["error_notified"] = False


def _safe_notify(fn, *args) -> None:
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 – ntfy-Ausfall darf den Checker nicht stoppen
        log.error("ntfy-Benachrichtigung fehlgeschlagen: %s", exc)


def run(cfg: Config) -> int:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    state_file = cfg.data_dir / "state.json"

    hostel_id, hostel_name = cfg.hostel_id, cfg.hostel_name
    key = None
    state: dict = {}
    started = False

    while True:
        exit_code = 1
        try:
            if not hostel_id:
                hostel_id, hostel_name = resolve_hostel_id(cfg.hostel_name, cfg.timeout)
                log.info("Jugendherberge %r aufgelöst zu ID %s", hostel_name, hostel_id)

            if key is None:
                key = f"{hostel_id}|{cfg.arrival}|{cfg.departure}|{sorted(cfg.persons.items())}"
                loaded = load_state(state_file)
                if loaded.get("key") != key:
                    loaded = {"key": key, "available": None}
                if state.get("error_count"):
                    for k in ("error_count", "error_notified", "last_error", "last_error_at"):
                        loaded[k] = state.get(k)
                state = loaded

            if not started:
                started = True
                log.info(
                    "Prüfe %s (ID %s), %s – %s, %d Person(en), Intervall %ds",
                    hostel_name or "?", hostel_id, cfg.arrival, cfg.departure, cfg.total_persons, cfg.interval,
                )
                if cfg.notify_startup:
                    _safe_notify(send_ntfy, cfg, "DJH-Checker gestartet",
                                 f"Überwache {hostel_name or hostel_id} vom {cfg.arrival} bis {cfg.departure} "
                                 f"für {cfg.total_persons} Person(en).", None, "low", "eyes")

            result, url = check(cfg, hostel_id, hostel_name)
            hostel_name = result.hostel_name or hostel_name
            log.info("%s: %s", result.hostel_name, describe(result))
            _safe_notify(handle_recovery, cfg, state)

            previous = state.get("available")
            new_available = result.available
            try:
                if result.available and previous is not True:
                    lines = [f"{o.title}: {o.price}" for o in result.bookable_offers]
                    send_ntfy(
                        cfg,
                        f"DJH {result.hostel_name} verfügbar!",
                        f"{cfg.arrival} – {cfg.departure}, {cfg.total_persons} Person(en)\n" + "\n".join(lines),
                        click=url,
                        tags="tada,house",
                    )
                elif not result.available and previous is True and cfg.notify_unavailable:
                    send_ntfy(cfg, f"DJH {result.hostel_name} nicht mehr verfügbar",
                              f"{cfg.arrival} – {cfg.departure}", click=url, priority="default", tags="x")
            except Exception as exc:  # noqa: BLE001
                log.error("ntfy-Benachrichtigung fehlgeschlagen, nächster Versuch beim nächsten Check: %s", exc)
                new_available = previous

            state.update(available=new_available, last_check=datetime.now().isoformat(timespec="seconds"),
                         last_status=describe(result))
            exit_code = 0 if result.available else 2
        except CheckError as exc:
            _safe_notify(handle_error, cfg, state, exc)
        except LookupError:
            raise
        except Exception as exc:  # noqa: BLE001 – der Dauerlauf soll nie abstürzen
            log.exception("Unerwarteter Fehler")
            _safe_notify(handle_error, cfg, state, CheckError("schema", f"Unerwarteter Fehler: {exc!r}"))

        if key is not None:
            try:
                save_state(state_file, state)
            except OSError as exc:
                log.error("Konnte Status nicht speichern: %s", exc)

        if cfg.run_once:
            return exit_code
        time.sleep(cfg.interval)




def main() -> int:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        cfg = load_config()
        return run(cfg)
    except (ValueError, LookupError) as exc:
        log.error("Konfigurationsfehler: %s", exc)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
