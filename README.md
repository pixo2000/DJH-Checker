# DJH-Checker

Prüft regelmäßig, ob eine bestimmte Jugendherberge auf [jugendherberge.de](https://www.jugendherberge.de) für einen Zeitraum und eine Personenzahl buchbar ist. Wird sie verfügbar, kommt eine Benachrichtigung per [ntfy](https://ntfy.sh).

## Schnellstart (Docker)

```bash
cp .env.example .env      # Werte anpassen
docker compose up -d --build
docker compose logs -f
```

Die ntfy-App installieren und das in `NTFY_TOPIC` gesetzte Topic abonnieren. Der Topic-Name sollte schwer zu erraten sein, weil Topics auf ntfy.sh öffentlich sind.

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Beschreibung |
|---|---|---|
| `DJH_NAME` | – | Name der Jugendherberge, z. B. `Köln-Deutz` (muss eindeutig sein) |
| `DJH_ID` | – | Alternativ direkt die ID (z. B. `450`) |
| `ARRIVAL` / `DEPARTURE` | – | Anreise/Abreise, `TT.MM.JJJJ` oder `JJJJ-MM-TT` |
| `PERSONS_16`, `PERSONS_17`, `PERSONS_18_26`, `PERSONS_27_PLUS` | 0 | Personen nach Altersgruppe |
| `PERSONS` | – | Kurzform: Anzahl Personen (18–26 J.), falls keine der obigen gesetzt ist |
| `CHECK_INTERVAL` | 1800 | Prüfintervall in Sekunden (min. 60) |
| `NTFY_URL` | `https://ntfy.sh` | ntfy-Server |
| `NTFY_TOPIC` | – | ntfy-Topic (ohne Topic: nur Logausgabe) |
| `NTFY_TOKEN` | – | Optionales Access-Token |
| `NTFY_PRIORITY` | `high` | ntfy-Priorität |
| `NOTIFY_UNAVAILABLE` | `false` | Auch melden, wenn es wieder ausgebucht ist |
| `NOTIFY_STARTUP` | `false` | Beim Start eine Nachricht senden |
| `ERROR_NOTIFY_AFTER` | 2 | Netzwerkfehler (Timeout, Verbindungsfehler, HTTP 5xx) erst nach so vielen Fehlern in Folge melden |
| `ANOMALY_NOTIFY_AFTER` | 1 | Blockade (HTTP 401/403/429, Captcha) oder unerwartete Antwort nach so vielen Fehlern in Folge melden |
| `DATA_DIR` | `./data` (Docker: `/data`) | Hier liegen `last_response.html` (letzte Antwort), `error_response.html` (letzte fehlerhafte Antwort) und `state.json` |
| `RUN_ONCE` | `false` | Nur einmal prüfen. Exit-Code: 0 = verfügbar, 2 = nicht verfügbar, 1 = Fehler, 3 = Konfigurationsfehler |
| `LOG_LEVEL` | `INFO` | Log-Level |

Benachrichtigt wird nur beim Wechsel von „nicht verfügbar“ zu „verfügbar“. Der Zustand wird in `state.json` gespeichert, deshalb gibt es nach einem Neustart keine doppelten Nachrichten.

### Fehler-Benachrichtigungen

Jede Antwort wird streng geprüft. Weicht sie vom erwarteten Aufbau ab, gibt es eine Warnung (Priorität `high`):

- **Seite nicht erreichbar:** Timeouts, Verbindungsfehler oder HTTP 5xx. Gemeldet wird erst nach `ERROR_NOTIFY_AFTER` Fehlern in Folge.
- **Zugriff blockiert?:** HTTP 401/403/429 oder eine Captcha-/Bot-Schutz-Seite, also z. B. eine IP-Sperre.
- **Unerwartete Antwort:** Keine Suchergebnisse, die Herberge fehlt, fehlende oder kaputte Angebotsdaten. Das deutet oft auf eine geänderte Webseite hin.

Pro Fehlerserie kommt nur eine Warnung. Klappt die Prüfung danach wieder, folgt die Nachricht „DJH-Checker funktioniert wieder“. Der Checker läuft bei Fehlern einfach weiter, und die fehlerhafte Antwort wird in `error_response.html` gespeichert.

## Automatischer Build & Release (GitHub Actions)

Der Workflow [.github/workflows/docker.yml](.github/workflows/docker.yml) baut das Image für `linux/amd64` und `linux/arm64` (z. B. Raspberry Pi) und veröffentlicht es in der GitHub Container Registry:

| Ereignis | Ergebnis |
|---|---|
| Push auf `main`/`master` | Image `ghcr.io/<user>/<repo>:latest` sowie `:sha-xxxxxxx` |
| Tag `v1.2.3` pushen | Images `:1.2.3`, `:1.2`, `:1` und zusätzlich ein GitHub-Release mit automatisch erzeugten Release Notes |
| Pull Request | Nur ein Test-Build ohne Push |

Ein Release erstellst du so:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Nach dem ersten Push solltest du das Paket unter *GitHub → Profil → Packages → Package settings* auf **Public** stellen, sonst ist für `docker pull` ein Login nötig. Danach kannst du in `docker-compose.yml` `image: ghcr.io/<user>/<repo>:latest` statt `build: .` verwenden.

## Ohne Docker

Es wird nur Python 3.9+ ohne weitere Pakete benötigt.

```powershell
$env:DJH_NAME="Köln-Deutz"; $env:ARRIVAL="23.08.2027"; $env:DEPARTURE="30.08.2027"
$env:PERSONS_18_26="2"; $env:NTFY_TOPIC="mein-topic"
python djh_checker.py
```
