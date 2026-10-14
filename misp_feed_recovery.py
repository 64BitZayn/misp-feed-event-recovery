#!/usr/bin/env python3
"""
misp-feed-event-recovery

Some MISP feeds (large abuse.ch feeds were the worst for me) are so slow that a
full "Fetch all events" times out or stalls. This tool works around that: it
collects the event UUIDs listed in the feed's preview pages and asks MISP to
pull each event individually, via the same endpoint as the per-event "Fetch"
button in the feed preview (/feeds/getEvent/<feed_id>/<uuid>).

Usage:
  export MISP_KEY=...            # API key with permission to fetch feeds
  python3 misp_feed_recovery.py --url https://misp.example.org --feed-id 65 --pages ./feed65_pages

The --pages folder holds the feed's preview index pages saved from the browser
(Feeds -> magnifier icon -> save each page as HTML).
"""
import argparse
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def collect_uuids(pages_dir, feed_id):
    """Pull event UUIDs out of saved feed preview pages.

    Depending on the MISP version and the user's permissions, a preview page lists
    events either as "Fetch" forms (/feeds/getEvent/<id>/<uuid>) or only as preview
    links (/feeds/previewEvent/<id>/<uuid>). Both are collected.
    """
    uuids, pages = set(), 0
    pattern = re.compile(rf"/feeds/(?:getEvent|previewEvent)/{feed_id}/([0-9a-fA-F-]{{36}})")
    for name in sorted(os.listdir(pages_dir)):
        if not name.lower().endswith((".html", ".htm")):
            continue
        pages += 1
        with open(os.path.join(pages_dir, name), encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        for tag in soup.find_all(["form", "a"]):
            link = tag.get("action") or tag.get("href") or ""
            match = pattern.search(link)
            if match and UUID_RE.match(match.group(1)):
                uuids.add(match.group(1).lower())
    return sorted(uuids), pages


def fetch_event(session, base_url, feed_id, uuid, retries, timeout):
    url = f"{base_url}/feeds/getEvent/{feed_id}/{uuid}"
    for attempt in range(1, retries + 1):
        try:
            r = session.post(url, timeout=timeout)
            if r.status_code == 200:
                return True, r.text
            error = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            error = type(e).__name__
        if attempt < retries:
            time.sleep(2 ** attempt)  # back off: 2 s, 4 s, 8 s ...
    return False, error


def main(argv=None):
    p = argparse.ArgumentParser(description="Fetch MISP feed events one by one from saved preview pages.")
    p.add_argument("--url", required=True, help="MISP base URL, e.g. https://misp.example.org")
    p.add_argument("--feed-id", required=True, type=int, help="numeric feed ID (Sync Actions -> Feeds)")
    p.add_argument("--pages", required=True, help="folder with the saved feed preview HTML pages")
    p.add_argument("--out", default="fetched_events", help="where to store each response (default: fetched_events/)")
    p.add_argument("--key", default=os.environ.get("MISP_KEY"), help="API key (default: $MISP_KEY)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed lab certs only)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=120, help="seconds per request")
    p.add_argument("--dry-run", action="store_true", help="only list the UUIDs that would be fetched")
    args = p.parse_args(argv)

    if not args.key and not args.dry_run:
        p.error("no API key: set MISP_KEY or pass --key")

    uuids, pages = collect_uuids(args.pages, args.feed_id)
    print(f"[*] {len(uuids)} event UUID(s) found in {pages} page(s) for feed {args.feed_id}")
    if args.dry_run or not uuids:
        for u in uuids:
            print("   ", u)
        return 0

    os.makedirs(args.out, exist_ok=True)
    session = requests.Session()
    session.headers.update({"Authorization": args.key, "Accept": "application/json"})
    session.verify = not args.insecure
    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    done = skipped = failed = 0
    for i, uuid in enumerate(uuids, 1):
        target = os.path.join(args.out, f"{uuid}.json")
        if os.path.exists(target):  # makes re-runs (e.g. from cron) resume where they stopped
            skipped += 1
            continue
        ok, body = fetch_event(session, args.url.rstrip("/"), args.feed_id, uuid, args.retries, args.timeout)
        if ok:
            with open(target, "w", encoding="utf-8") as f:
                f.write(body)
            done += 1
            print(f"  [{i}/{len(uuids)}] OK    {uuid}")
        else:
            failed += 1
            print(f"  [{i}/{len(uuids)}] FAIL  {uuid} ({body})")

    summary = {"feed_id": args.feed_id, "fetched": done, "skipped_existing": skipped, "failed": failed}
    print("[*] " + json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
