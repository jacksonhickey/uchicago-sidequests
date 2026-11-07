#!/usr/bin/env python3
"""Scrape events from events.uchicago.edu (LiveWhale JSON API) and do312.com (all days).

Each event records: title, start/end time, location, address, description, image,
categories, URL. Duplicates are removed within a run and across runs (SQLite).

Usage:
    python scrape_events.py                    # next 30 days UChicago, all Do312 pages
    python scrape_events.py --days 14 --max-pages 10
    python scrape_events.py --no-details       # skip Do312 per-event page fetch (faster)

Outputs events.json and events.csv; state kept in events.db (so reruns add only new events).
Requires: pip install requests
"""
import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; uchicago-sidequests-scraper/1.0)"}
UC_API = "https://events.uchicago.edu/live/json/events"
UC_FIELDS = ("id,title,url,date_iso,date2_iso,is_all_day,is_canceled,location,summary,"
             "description,thumbnail,event_types,event_types_audience,event_types_campus,cost")
DO_BASE = "https://do312.com/"

session = requests.Session()
session.headers.update(HEADERS)


def get(url, **kw):
    r = session.get(url, timeout=30, **kw)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r


def clean(text):
    """Strip tags / entities / extra whitespace."""
    if not text:
        return ""
    text = re.sub(r"<(br|/p|/li)\s*/?>", "\n", str(text), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def as_list(v):
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def make_event(**kw):
    ev = dict(source="", title="", start="", end="", all_day=False, location="", address="",
              description="", image="", categories=[], url="", ticket_url="", price="", age="", canceled=False,
              lat=None, lng=None, walk_min=None, bike_min=None, transit_min=None,
              bike_transit_min=None, via_min=None, via_transit_min=None, via_leg_min=None, via_note="", best_mode="", best_min=None, transit_route="")
    ev.update(kw)
    if not ev["age"]:
        ev["age"] = find_age(ev["title"], ev["price"], ev["description"])
    return ev


AGE_RE = re.compile(
    r"\ball[\s-]ages\b"
    r"|\bages?\s*(\d{1,2})\s*(?:\+|and\s+(?:up|over|older)|&\s*(?:up|over|older)|plus)"
    r"|\b(\d{1,2})\s*(?:\+|and\s+(?:up|over|older)|&\s*(?:up|over|older)|plus)(?![\w$%])"
    r"|\b(\d{1,2})\s*(?:years?|yrs?)\s*(?:old\s*)?(?:and\s+)?(?:\+|or\s+older|and\s+(?:up|over))",
    re.I)


def find_age(*texts):
    """Return an age requirement like '21+' or 'All ages' if any text states one."""
    for t in texts:
        for m in AGE_RE.finditer(t or ""):
            if m.group(0).lower().replace("-", " ").startswith("all"):
                return "All ages"
            n = next(int(g) for g in m.groups() if g)
            if 16 <= n <= 25:  # ignore things like "100+ vendors" or "3+ shows"
                return f"{n}+"
    return ""


# ---------------------------------------------------------------- UChicago
def scrape_uchicago(days):
    start = date.today()
    end = start + timedelta(days=days)
    url = (f"{UC_API}/start_date/{start:%Y-%m-%d}/end_date/{end:%Y-%m-%d}"
           f"/max/1000/response_fields/{UC_FIELDS}")
    events, page = [], 1
    while True:
        j = get(url + (f"?page={page}" if page > 1 else "")).json()
        for d in j.get("data", []):
            thumb = d.get("thumbnail") or ""
            # thumbnail is a 150px crop; request a larger uncropped version
            thumb = re.sub(r"/width/\d+/height/\d+/crop/1", "/width/800", thumb)
            cats = as_list(d.get("event_types")) + as_list(d.get("event_types_audience")) \
                + as_list(d.get("event_types_campus"))
            events.append(make_event(
                source="events.uchicago.edu", title=clean(d.get("title")),
                start=d.get("date_iso") or "", end=d.get("date2_iso") or "",
                all_day=bool(d.get("is_all_day")), location=clean(d.get("location")),
                lat=d.get("location_latitude") or None, lng=d.get("location_longitude") or None,
                description=clean(d.get("description") or d.get("summary")),
                image=thumb, categories=cats, url=d.get("url") or "",
                price=" ".join(clean(d.get("cost")).split()), canceled=bool(d.get("is_canceled"))))
        if page >= j.get("meta", {}).get("total_pages", 1):
            break
        page += 1
    return events


# ---------------------------------------------------------------- Do312
class CardParser(HTMLParser):
    """Parse Do312 listing cards: date heading -> card (href, title, image, text lines)."""

    def __init__(self):
        super().__init__()
        self.cards, self.cur, self.day = [], None, ""
        self.p_class = None
        self.in_h3 = False
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and (a.get("aria-labelledby") or "").startswith("event-date-"):
            self.day = a["aria-labelledby"][len("event-date-"):]
        if tag == "div" and a.get("data-card-kind") == "event":
            self.cur = {"day": self.day, "href": "", "title": "", "image": "", "lines": []}
            self.depth = 1
            return
        if self.cur is None:
            return
        if tag == "div":
            self.depth += 1
        elif tag == "a" and a.get("class", "").startswith("card-link") and not self.cur["href"]:
            self.cur["href"] = a.get("href", "")
            self.cur["title"] = a.get("aria-label", "")
        elif tag == "img" and not self.cur["image"]:
            self.cur["image"] = a.get("src", "")
        elif tag == "h3":
            self.in_h3 = True
        elif tag == "p":
            self.p_class = a.get("class", "")
            self.cur["lines"].append(["", self.p_class])

    def handle_endtag(self, tag):
        if self.cur is None:
            return
        if tag == "h3":
            self.in_h3 = False
        elif tag == "p":
            self.p_class = None
        elif tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.cards.append(self.cur)
                self.cur = None

    def handle_data(self, data):
        if self.cur is not None and self.p_class is not None and self.cur["lines"]:
            self.cur["lines"][-1][0] += data
        elif self.cur is not None and self.in_h3 and not self.cur["title"]:
            self.cur["title"] += data


def parse_time(text):
    """'Today | 10 pm' / 'Sat | 7:30 pm' -> '22:00' ; returns '' if absent."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])m", text, re.I)
    if not m:
        return ""
    h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "p":
        h += 12
    return f"{h:02d}:{mi:02d}"


def chicago_offset(day):
    """Offset string for America/Chicago on a YYYY-MM-DD (DST-aware)."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/Chicago")).strftime("%z")[:3] + ":00"
    except Exception:
        return "-06:00"


def do312_event_from_card(card, page_url):
    link = urljoin(page_url, card["href"])
    day = card["day"] or (re.search(r"/events/(\d{4})/(\d+)/(\d+)/", link) and
                          "{}-{:02d}-{:02d}".format(*map(int, re.search(r"/events/(\d{4})/(\d+)/(\d+)/", link).groups())))
    date_line = next((t for t, c in card["lines"] if "date" in c.split()), "")
    enrich = next((t for t, c in card["lines"] if "enrichment" in c.split()), "")
    venue = next((t.strip() for t, c in card["lines"] if "date" not in c.split() and "enrichment" not in c.split()), "")
    tm = parse_time(date_line)
    start = f"{day}T{tm}:00{chicago_offset(day)}" if (day and tm) else (day or "")
    # enrichment looks like "Music · Pilsen · Electronic / Dance": category · neighborhood · genre
    parts = [p.strip() for p in re.split(r"[·•·�]", enrich) if p.strip()]
    return make_event(source="do312.com", title=clean(card["title"]), start=start,
                      all_day=not tm, location=clean(venue), image=card["image"], url=link,
                      categories=parts)


PRICE_RE = re.compile(r"\$\s?\d|\bfree\b|\bdonation|\bpwyw\b|pay what", re.I)


def find_price(h):
    """Price from Do312 page data: free flag, ticket_info, or per-vendor info strings."""
    if re.search(r"free_event:true", h):
        return "Free"
    texts = [html.unescape(t) for t in re.findall(r'ticket_info:"((?:[^"\\]|\\.)*)"', h)]
    texts += [html.unescape(t) for t in re.findall(r'\binfo:"((?:[^"\\]|\\.)*)"', h)]
    for t in texts:
        if PRICE_RE.search(t):
            return t.strip()
    return ""


def enrich_do312(ev):
    """Fetch the event page's JSON-LD for description, address, ticket link, end time."""
    try:
        h = get(ev["url"]).text
    except requests.RequestException:
        return ev
    graph = []
    for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            j = json.loads(m)
        except ValueError:
            continue
        graph += j.get("@graph", [j]) if isinstance(j, dict) else j
    by_id = {n.get("@id"): n for n in graph if isinstance(n, dict)}
    # The visible page has the real description and ticket/price info (JSON-LD only has SEO text).
    m = re.search(r'data-event-description[^>]*>(.*?)<!---->\s*</div>', h, re.S)
    page_desc = clean(m.group(1)) if m else ""
    ev["price"] = ev["price"] or find_price(h)
    # ticket_info often holds the age rule (e.g. "18+ with Valid Government ID")
    ticket_texts = [html.unescape(t) for t in re.findall(r'ticket_info:"((?:[^"\\]|\\.)*)"', h)]
    ev["age"] = find_age(*ticket_texts, page_desc, ev["title"]) or ev["age"]
    node = next((n for n in graph if isinstance(n, dict) and n.get("@type") == "Event"), None)
    if not node:
        ev["description"] = page_desc
        return ev
    ev["description"] = page_desc or clean(node.get("description"))
    # Recurring events carry the whole series range in JSON-LD; keep the listing's
    # occurrence start and only accept an end time that falls on the same day.
    end = node.get("endDate") or ""
    if end[:10] == ev["start"][:10]:
        ev["end"] = end
    if not ev["start"]:
        ev["start"] = node.get("startDate") or ""
    ev["ticket_url"] = (node.get("offers") or {}).get("url", "") if isinstance(node.get("offers"), dict) else ""
    img = node.get("image")
    if img and not ev["image"]:
        ev["image"] = img[0] if isinstance(img, list) else img
    loc = node.get("location") or {}
    loc = by_id.get(loc.get("@id"), loc)
    addr = loc.get("address") or {}
    if isinstance(addr, dict):
        ev["address"] = ", ".join(x.strip() for x in (addr.get("streetAddress"), addr.get("addressLocality"),
                                                      addr.get("addressRegion"), addr.get("postalCode")) if x)
    if not ev["location"]:
        ev["location"] = loc.get("name", "")
    geo = loc.get("geo") or {}
    if geo.get("latitude") and geo.get("longitude"):
        ev["lat"], ev["lng"] = geo["latitude"], geo["longitude"]
    return ev


def scrape_do312_day(day, max_pages):
    """All listing cards for one day: /events/YYYY/M/D?page=N (20 per page) until nothing new."""
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{DO_BASE}events/{day.year}/{day.month}/{day.day}" + (f"?page={page}" if page > 1 else "")
        try:
            p = CardParser()
            p.feed(get(url).text)
        except requests.RequestException:
            break
        new = [c for c in p.cards if c["href"] not in seen]
        if not new:
            break
        for c in new:
            seen.add(c["href"])
            c["day"] = c["day"] or day.isoformat()
            out.append(do312_event_from_card(c, url))
    print(f"  do312 {day}: {len(out)} events", file=sys.stderr)
    return out


def scrape_do312(days, max_pages, details):
    """Walk each day's listing for the next `days` days, then enrich each event from its page."""
    start = date.today()
    with ThreadPoolExecutor(4) as ex:
        per_day = list(ex.map(lambda d: scrape_do312_day(start + timedelta(days=d), max_pages), range(days)))
    events = [e for day in per_day for e in day]
    if details and events:
        with ThreadPoolExecutor(8) as ex:
            events = list(ex.map(enrich_do312, events))
    return events


# ---------------------------------------------------------------- dedupe / storage
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe_key(ev):
    """Same title + same day + same venue => same event (works across both sources
    and for repeated listings of one event)."""
    return hashlib.sha1(f"{norm(ev['title'])}|{ev['start'][:10]}|{norm(ev['location'])}".encode()).hexdigest()


BAD_LOCATION_RE = re.compile(
    r"log\s*in|sign\s*in|login|to view (?:details|location)|^\s*(?:tba|tbd|n/?a|none)\s*$"
    r"|^\s*(?:the university of chicago,\s*)?various(?: locations?)?\s*$", re.I)


def has_real_location(ev):
    loc = ev["location"].strip()
    return bool(loc) and not BAD_LOCATION_RE.search(loc)


def store(events, db_path):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, data TEXT)")
    new = skipped = 0
    for ev in events:
        k = dedupe_key(ev)
        row = con.execute("SELECT data FROM events WHERE key=?", (k,)).fetchone()
        if row is None:
            con.execute("INSERT INTO events VALUES (?,?)", (k, json.dumps(ev)))
            new += 1
        else:
            old = json.loads(row[0])
            # merge: fill blanks and union categories instead of storing a duplicate
            for f, v in ev.items():
                if f == "categories":
                    old[f] = list(dict.fromkeys(old[f] + v))
                elif not old.get(f) and v:
                    old[f] = v
            con.execute("UPDATE events SET data=? WHERE key=?", (json.dumps(old), k))
            skipped += 1
    con.commit()
    all_events = [e for e in (json.loads(r[0]) for r in con.execute("SELECT data FROM events"))
                  if has_real_location(e)]
    con.close()
    return sorted(all_events, key=lambda e: e["start"]), new, skipped


def export(events):
    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)
    cols = list(make_event().keys())
    with open("events.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for e in events:
            w.writerow({**e, "categories": "; ".join(e["categories"])})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="days ahead to scrape from both sites (default 14)")
    ap.add_argument("--max-pages", type=int, default=50, help="max Do312 listing pages per day (20 events each)")
    ap.add_argument("--no-details", action="store_true", help="skip Do312 per-event page fetch")
    ap.add_argument("--db", default="events.db")
    ap.add_argument("--travel", choices=["auto", "on", "off"], default="auto",
                    help="add travel times from the Quad via a local OpenTripPlanner server "
                         "(auto = only if the server is running)")
    ap.add_argument("--otp-url", default=os.environ.get("OTP_URL", "http://localhost:8080"))
    args = ap.parse_args()

    batch = []
    for name, fn in (("UChicago", lambda: scrape_uchicago(args.days)),
                     ("Do312", lambda: scrape_do312(args.days, args.max_pages, not args.no_details))):
        try:
            got = fn()
            print(f"{name}: {len(got)} events scraped", file=sys.stderr)
            batch += got
        except requests.RequestException as e:
            print(f"{name}: failed ({e})", file=sys.stderr)

    before = len(batch)
    batch = [e for e in batch if has_real_location(e)]
    print(f"Dropped {before - len(batch)} events with no usable location", file=sys.stderr)
    events, new, merged = store(batch, args.db)
    today = date.today().isoformat()
    events = [e for e in events if e["start"][:10] >= today]  # drop events that already happened
    if args.travel != "off":
        from travel import add_travel_times
        events = add_travel_times(events, args.otp_url)
    export(events)
    print(f"{new} new, {merged} duplicates merged, {len(events)} total -> events.json / events.csv")


if __name__ == "__main__":
    main()
