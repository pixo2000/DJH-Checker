"""HTTP-GET ausführen: python get_domain.py "https://example.com/pfad"."""

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(
        description="Ruft eine Domain oder URL per GET ab und gibt den Inhalt aus."
    )
    parser.add_argument("domain", help="Domain oder vollständige URL")
    args = parser.parse_args()

    url = args.domain.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            parser.error("Bitte eine gültige HTTP- oder HTTPS-URL angeben.")
        url = urlunsplit(
            (
                parts.scheme,
                parts.netloc.encode("idna").decode("ascii"),
                quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                quote(parts.query, safe="=&%/:?@!$'()*+,;+-._~"),
                "",
            )
        )
    except ValueError as error:
        parser.error(f"Ungültige URL: {error}")

    try:
        with urlopen(url, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            print(response.read().decode(charset, errors="replace"))
    except HTTPError as error:
        with error:
            charset = error.headers.get_content_charset() or "utf-8"
            print(error.read().decode(charset, errors="replace"))
        print(f"HTTP-Fehler: {error.code} {error.reason}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError, ValueError) as error:
        print(f"Anfrage fehlgeschlagen: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
