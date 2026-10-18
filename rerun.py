#!/usr/bin/env python3
"""
Radio Derb Reruns: builds a podcast feed that "airs" random old Radio Derb
episodes from John Derbyshire's archive, a few at a time, so Overcast (or any
podcast app) sees them arrive like new episodes.

Each run picks EPISODES_PER_RUN random episodes that haven't aired yet, adds
them to state.json, and rewrites feed.xml. Audio is linked straight from
johnderbyshire.com, nothing is re-hosted.
"""
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

ARCHIVE_URL = "https://www.johnderbyshire.com/Opinions/RadioDerb/page.html"
ARTWORK_URL = "https://www.johnderbyshire.com/Opinions/RadioDerb/wireless.jpg"

EPISODES_PER_RUN = int(os.environ.get("EPISODES_PER_RUN", "1"))
SEED_EPISODES = int(os.environ.get("SEED_EPISODES", "3"))   # first run only
MAX_FEED_ITEMS = int(os.environ.get("MAX_FEED_ITEMS", "60"))
FEED_TITLE = os.environ.get("FEED_TITLE", "Radio Derb Reruns")

STATE = Path("state.json")
FEED = Path("feed.xml")
HEADERS = {"User-Agent": "RadioDerbReruns/1.0 (personal podcast feed)"}
EP_LINK = re.compile(r"(?:^|/)(\d{4}-\d{2}-\d{2})\.html$")


def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def load_archive():
    """Return {date: {"page": url, "topics": [..]}} for every episode listed."""
    soup = BeautifulSoup(get(ARCHIVE_URL).text, "html.parser")
    episodes = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        m = EP_LINK.search(href)          # links with #fragments are topic links; skip
        if not m or m.group(1) in episodes:
            continue
        date = m.group(1)
        topics = []
        row = a.find_parent("tr")
        if row:
            cells = row.find_all("td")
            if len(cells) > 1:
                topics = [t.get_text(" ", strip=True) for t in cells[1].find_all("a")]
        topics = [t for t in topics
                  if t and not re.match(r"^(intro|signoff)\b", t, re.I)]
        episodes[date] = {"page": urljoin(ARCHIVE_URL, href), "topics": topics}
    return episodes


def find_audio(page_url):
    """Return (audio_url, mime) from an episode transcript page, or (None, None)."""
    soup = BeautifulSoup(get(page_url).text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].lower()
        if href.endswith(".mp3"):
            return urljoin(page_url, a["href"]), "audio/mpeg"
        if href.endswith(".m4a"):
            return urljoin(page_url, a["href"]), "audio/mp4"
    return None, None


def audio_length(url):
    try:
        r = requests.head(url, headers=HEADERS, timeout=30, allow_redirects=True)
        if r.ok:
            return int(r.headers.get("Content-Length", 0))
    except requests.RequestException:
        pass
    return 0


def pretty(date):
    d = datetime.strptime(date, "%Y-%m-%d")
    return f"{d:%b} {d.day}, {d.year}"


def air_one(archive, state):
    pool = [d for d in archive
            if d not in state["skipped"] and d not in state["aired_this_cycle"]]
    if not pool:                           # heard them all: start the cycle over
        state["cycle"] += 1
        state["aired_this_cycle"] = []
        pool = [d for d in archive if d not in state["skipped"]]
    random.shuffle(pool)
    for date in pool[:10]:
        ep = archive[date]
        url, mime = find_audio(ep["page"])
        if not url:
            state["skipped"].append(date)  # e.g. the transcript-only 4/22/2011
            continue
        topics = ep["topics"]
        title = pretty(date)
        if topics:
            title += ": " + ", ".join(topics[:3]) + ("…" if len(topics) > 3 else "")
        return {
            "date": date,
            "guid": f"radio-derb-rerun-{date}-c{state['cycle']}",
            "title": title,
            "topics": topics,
            "page": ep["page"],
            "audio": url,
            "mime": mime,
            "length": audio_length(url),
        }
    return None


def write_feed(state):
    items = []
    for e in reversed(state["aired"][-MAX_FEED_ITEMS:]):
        desc = (f"Originally aired {pretty(e['date'])}.\n\n"
                + ("Segments: " + "; ".join(e["topics"]) + "\n\n" if e["topics"] else "")
                + f"Transcript: {e['page']}")
        items.append(f"""    <item>
      <title>{escape(e['title'])}</title>
      <guid isPermaLink="false">{escape(e['guid'])}</guid>
      <pubDate>{e['pubDate']}</pubDate>
      <link>{escape(e['page'])}</link>
      <description>{escape(desc)}</description>
      <enclosure url="{escape(e['audio'])}" length="{e['length']}" type="{e['mime']}"/>
    </item>""")
    FEED.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{ARCHIVE_URL}</link>
    <description>Random episodes from the Radio Derb archive (2004–2025), resurfaced one at a time.</description>
    <language>en-us</language>
    <itunes:author>John Derbyshire</itunes:author>
    <itunes:image href="{ARTWORK_URL}"/>
    <image><url>{ARTWORK_URL}</url><title>{escape(FEED_TITLE)}</title><link>{ARCHIVE_URL}</link></image>
    <lastBuildDate>{format_datetime(datetime.now(timezone.utc))}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
""", encoding="utf-8")


def main():
    state = (json.loads(STATE.read_text()) if STATE.exists()
             else {"aired": [], "skipped": [], "cycle": 1, "aired_this_cycle": []})
    archive = load_archive()
    print(f"Archive lists {len(archive)} episodes; {len(state['aired'])} aired so far.")

    count = SEED_EPISODES if not state["aired"] else EPISODES_PER_RUN
    for i in range(count):
        ep = air_one(archive, state)
        if not ep:
            break
        # stagger timestamps so seeded episodes keep a stable order
        ep["pubDate"] = format_datetime(datetime.fromtimestamp(time.time() + i, timezone.utc))
        state["aired"].append(ep)
        state["aired_this_cycle"].append(ep["date"])
        print("Aired:", ep["title"])

    STATE.write_text(json.dumps(state, indent=1))
    write_feed(state)


if __name__ == "__main__":
    main()
