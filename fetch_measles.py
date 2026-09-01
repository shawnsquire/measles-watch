#!/usr/bin/env python3
"""Measles watch: fetch official health-department pages and regional news,
diff against the previous run, write site/data.json, and (optionally) push
a notification via ntfy when something new shows up.

Stdlib only. Designed to run on a schedule (GitHub Actions cron) with the
repo itself as the state store: state.json and site/data.json get committed
after each run, and Netlify redeploys the static site.

Env vars (all optional):
  NTFY_TOPIC   - if set, POST alerts to {NTFY_SERVER}/{NTFY_TOPIC}
  NTFY_SERVER  - defaults to https://ntfy.sh (point at self-hosted ntfy if you have one)
"""

import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
DATA_PATH = ROOT / "site" / "data.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
TIMEOUT = 30
MAX_NEWS_PER_QUERY = 8

# Only alert on new lines from watch pages that look measles-relevant...
RELEVANT = re.compile(
    r"\b(case|cases|exposure|exposures|wastewater|outbreak|confirmed|measles|mmr|quarantine)\b",
    re.I,
)
# ...and never on rotating banner noise (heat warnings, weather, nav chrome).
NOISE = re.compile(
    r"(heat warning|weather service|daytime highs|drink water|air-conditioned"
    r"|cookie|javascript|sign in|skip to)",
    re.I,
)

REGIONS = [
    {
        "name": "Pasadena & Bowie, MD",
        "news_queries": [
            'measles Maryland',
            'measles "Anne Arundel" OR "Prince George\'s"',
        ],
        "watch_pages": [
            {
                "name": "MDH measles page",
                "url": "https://health.maryland.gov/phpa/OIDEOR/IMMUN/pages/measles.aspx",
            },
            {
                "name": "MDH measles hub",
                "url": "https://health.maryland.gov/measles/Pages/default.aspx",
            },
        ],
    },
    {
        "name": "Sedona, AZ (Yavapai / Coconino)",
        "news_queries": [
            'measles Sedona OR Yavapai OR Coconino',
            'measles Arizona',
        ],
        "watch_pages": [
            {
                "name": "AZDHS measles page (updates Tue 3pm MST)",
                "url": "https://www.azdhs.gov/preparedness/epidemiology-disease-control/measles/index.php",
            },
            {
                "name": "Coconino County measles page",
                "url": "https://www.coconino.az.gov/3492/Measles-Information-and-Prevention",
            },
            {
                "name": "Yavapai County Community Health Services",
                "url": "https://www.yavapaiaz.gov/CHS",
            },
        ],
    },
]

CDC_URL = "https://www.cdc.gov/measles/data-research/index.html"


# ---------------------------------------------------------------- utilities

def fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a dead source shouldn't kill the run
        print(f"[warn] fetch failed {url}: {exc}", file=sys.stderr)
        return None


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data.strip())


def page_lines(html: str) -> list[str]:
    p = _TextExtractor()
    p.feed(html)
    lines, seen = [], set()
    for chunk in p.chunks:
        line = re.sub(r"\s+", " ", chunk).strip()
        if len(line) < 25 or NOISE.search(line):
            continue
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def google_news(query: str) -> list[dict]:
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    raw = fetch(url)
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        print(f"[warn] rss parse failed for {query!r}: {exc}", file=sys.stderr)
        return []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src = item.find("source")
        source = src.text.strip() if src is not None and src.text else ""
        if title and link:
            items.append({"title": title, "link": link, "published": pub, "source": source})
        if len(items) >= MAX_NEWS_PER_QUERY:
            break
    return items


def cdc_count(html: str | None) -> dict | None:
    if not html:
        return None
    m = re.search(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}),?\s*([\d,]+)\s*confirmed", html)
    if not m:
        return None
    return {"as_of": m.group(1), "cases": m.group(2), "url": CDC_URL}


def ntfy(title: str, body: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{server}/{topic}",
        data=body.encode(),
        headers={"Title": title, "Priority": "default", "Tags": "microscope"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=TIMEOUT).read()
        print(f"[ok] ntfy sent: {title}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] ntfy failed: {exc}", file=sys.stderr)


# ------------------------------------------------------------------- main

def main() -> None:
    state = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    page_state = state.get("pages", {})       # url -> {hash, lines}
    seen_links = set(state.get("seen_links", []))
    first_run = not state

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    alerts: list[str] = []
    out_regions = []

    for region in REGIONS:
        r_out = {"name": region["name"], "watch": [], "news": []}

        for wp in region["watch_pages"]:
            html = fetch(wp["url"])
            entry = {"name": wp["name"], "url": wp["url"], "ok": html is not None,
                     "changed": False, "new_lines": []}
            if html is not None:
                lines = page_lines(html)
                digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
                prev = page_state.get(wp["url"], {})
                if prev.get("hash") and prev["hash"] != digest:
                    added = [l for l in lines if l not in set(prev.get("lines", []))]
                    relevant = [l for l in added if RELEVANT.search(l)][:6]
                    if relevant:
                        entry["changed"] = True
                        entry["new_lines"] = relevant
                        alerts.append(f"{wp['name']} changed:\n" + "\n".join(f"- {l}" for l in relevant))
                page_state[wp["url"]] = {"hash": digest, "lines": lines[:400], "checked": now}
            r_out["watch"].append(entry)

        merged, dedupe = [], set()
        for q in region["news_queries"]:
            for item in google_news(q):
                if item["link"] in dedupe:
                    continue
                dedupe.add(item["link"])
                item["new"] = item["link"] not in seen_links
                merged.append(item)
                if item["new"] and not first_run:
                    alerts.append(f"News ({region['name']}): {item['title']}")
                seen_links.add(item["link"])
        r_out["news"] = merged[:12]
        out_regions.append(r_out)

    cdc = cdc_count(fetch(CDC_URL))

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(
        {"generated_at": now, "cdc": cdc, "regions": out_regions}, indent=1))

    STATE_PATH.write_text(json.dumps(
        {"pages": page_state, "seen_links": sorted(seen_links)[-800:], "last_run": now}, indent=1))

    if alerts and not first_run:
        ntfy("Measles watch: new activity", "\n\n".join(alerts[:10]))
    print(f"[ok] wrote {DATA_PATH} ({len(alerts)} alert item(s){', first run - no notify' if first_run else ''})")


if __name__ == "__main__":
    main()
