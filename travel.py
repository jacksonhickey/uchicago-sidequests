"""Estimate door-to-door travel time from the UChicago Main Quad to each event using a
local OpenTripPlanner (OTP) server (see otp_server.py).

Adds to each event (minutes): walk_min, bike_min, transit_min, bike_transit_min, via_min,
plus best_mode / best_min and a short transit_route summary like "Walk -> 6 -> Red Line".

Modes
  walk          walking only
  bike          biking only
  transit       CTA/Metra with walking to/from stops
  bike_transit  bike to a station/stop, then transit (bike left at the stop)
  via           RideSmart by Via (UChicago students, 5pm-4am). Pickup AND drop-off must be inside
                the service area in via_zone.json, so "via" is only offered when the destination
                itself is in the zone. Driving time + VIA_WAIT_MIN wait.
  via_transit   Via to a station inside the zone (Garfield Red/Green, 59th St Metra), then CTA/Metra.
"""
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

QUAD = (41.78970, -87.59970)  # Main Quad, University of Chicago (lat, lon)
VIA_WAIT_MIN = 8              # assumed wait for a Via/rideshare pickup
DEFAULT_EVENING = "19:00"     # used for all-day / time-less events
GEOCODE_CACHE = Path(__file__).parent / "geocode_cache.json"
TRAVEL_CACHE = Path(__file__).parent / "travel_cache.json"
UA = {"User-Agent": "uchicago-sidequests/1.0 (student events project)"}

MODES = {
    "walk": "[{mode: WALK}]",
    "bike": "[{mode: BICYCLE}]",
    "transit": "[{mode: TRANSIT}, {mode: WALK}]",
    "bike_transit": "[{mode: BICYCLE}, {mode: TRANSIT}]",
    "car": "[{mode: CAR}]",
}


# ------------------------------------------------------------------ geocoding
class Geocoder:
    def __init__(self):
        self.cache = json.loads(GEOCODE_CACHE.read_text("utf-8")) if GEOCODE_CACHE.exists() else {}
        self._last = 0.0

    def save(self):
        GEOCODE_CACHE.write_text(json.dumps(self.cache, indent=0, ensure_ascii=False), "utf-8")

    def _query(self, q):
        if q in self.cache:
            return self.cache[q]
        wait = 1.1 - (time.time() - self._last)  # Nominatim policy: max 1 request/second
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=30,
                             params={"q": q, "format": "json", "limit": 1,
                                     "viewbox": "-88.3,42.3,-87.3,41.4", "bounded": 1})
            data = r.json()
        except (requests.RequestException, ValueError):
            return None  # don't cache transient failures
        self.cache[q] = [float(data[0]["lat"]), float(data[0]["lon"])] if data else None
        return self.cache[q]

    def locate(self, ev):
        if ev.get("lat") and ev.get("lng"):
            return float(ev["lat"]), float(ev["lng"])
        loc = re.sub(r"\s+", " ", ev.get("location", "")).strip()
        tries = []
        if ev.get("address"):
            tries.append(ev["address"])
        if ev["source"] == "events.uchicago.edu":
            # campus buildings: "Ida Noyes Hall, Spiritual Life, 034" -> try progressively shorter
            parts = [p.strip() for p in loc.split(",") if p.strip()]
            for n in range(len(parts), 0, -1):
                tries.append(", ".join(parts[:n]) + ", University of Chicago, Chicago, IL")
        else:
            tries.append(f"{loc}, Chicago, IL")
        for q in tries:
            hit = self._query(q)
            if hit:
                return tuple(hit)
        return None


# ------------------------------------------------------------------ OTP client
class OTP:
    def __init__(self, base="http://localhost:8080"):
        self.url = base.rstrip("/") + "/otp/gtfs/v1"
        # Results are cached on disk. Transit depends on weekday + hour (schedules repeat weekly),
        # not the calendar date, so a venue routed once is reused on every later day. The cache
        # is dropped each month, when the CTA/Metra schedules get refreshed.
        self.memo = self._load()

    @staticmethod
    def _load():
        month = date.today().strftime("%Y-%m")
        raw = json.loads(TRAVEL_CACHE.read_text("utf-8")) if TRAVEL_CACHE.exists() else {}
        stale = raw.get("_stamp", month) != month  # no stamp = old-format file from this month
        memo = {"_stamp": month}
        for key, val in raw.items():
            if key == "_stamp":
                continue
            head, _, suffix = key.rpartition("|")
            if len(suffix) == 10 and suffix.isdigit():  # old format: YYYYMMDDHH -> weekday/hour
                d = datetime.strptime(suffix, "%Y%m%d%H")
                suffix = f"w{d.weekday()}h{d.hour}"
                key = f"{head}|{suffix}"
            if suffix and stale:
                continue  # schedule-dependent entry from a previous month
            memo.setdefault(key, val)
        return memo

    def save(self):
        TRAVEL_CACHE.write_text(json.dumps(dict(self.memo), separators=(",", ":")), "utf-8")

    def alive(self):
        try:
            return requests.post(self.url, json={"query": "{__typename}"}, timeout=5).ok
        except requests.RequestException:
            return False

    def plan(self, dest, mode, when, origin=QUAD):
        """Best itinerary (duration_s, route summary) or None. `when` = datetime to arrive by,
        or None to depart now-ish (used for time-insensitive modes)."""
        arrive = when is not None and mode in ("transit", "bike_transit")
        key = (f"{origin[0]:.4f},{origin[1]:.4f}|{dest[0]:.4f},{dest[1]:.4f}|{mode}|"
               f"{f'w{when.weekday()}h{when.hour}' if arrive else ''}")
        if key in self.memo:
            return self.memo[key]
        t = when if when else datetime.now() + timedelta(days=1)
        q = f"""{{ plan(from: {{lat: {origin[0]}, lon: {origin[1]}}}, to: {{lat: {dest[0]}, lon: {dest[1]}}},
            date: "{t:%Y-%m-%d}", time: "{t:%H:%M}", arriveBy: {str(arrive).lower()},
            transportModes: {MODES[mode]}, numItineraries: 3)
            {{ itineraries {{ duration legs {{ mode route {{ shortName longName }} }} }} }} }}"""
        result = None
        try:
            r = requests.post(self.url, json={"query": q}, timeout=60).json()
            its = ((r.get("data") or {}).get("plan") or {}).get("itineraries") or []
            if its:
                best = min(its, key=lambda i: i["duration"])
                result = (best["duration"], self._summary(best))
        except (requests.RequestException, ValueError):
            return None  # transient failure: don't cache it
        self.memo[key] = result
        return result

    @staticmethod
    def _summary(it):
        out = []
        for leg in it["legs"]:
            r = leg.get("route")
            name = (r.get("longName") or r.get("shortName")) if r else leg["mode"].title()
            if r and r.get("shortName") and r.get("longName") and leg["mode"] == "BUS":
                name = r["shortName"]  # e.g. bus "6" rather than "Jackson Park Express"
            if not out or out[-1] != name:
                out.append(name)
        return " -> ".join(out)


# ------------------------------------------------------------------ Via rules
VIA = json.loads((Path(__file__).parent / "via_zone.json").read_text("utf-8"))
VIA_START_HOUR, VIA_END_HOUR = 17, 4  # RideSmart by Via runs 5pm-4am daily, UChicago students only


def in_polygon(pt, poly):
    """Ray-casting point-in-polygon; pt and poly vertices are (lat, lon)."""
    y, x = pt
    inside = False
    for (y1, x1), (y2, x2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def in_via_zone(pt):
    return any(in_polygon(pt, poly) for poly in VIA["polygons"])


def via_open(depart):
    """True if a Via ride requested at `depart` falls inside service hours (5pm-4am)."""
    return depart is None or depart.hour >= VIA_START_HOUR or depart.hour < VIA_END_HOUR


def via_options(otp, dest, when, arrive_local):
    """RideSmart by Via can only pick up AND drop off inside its service area. So:
      via_min          Via all the way to the destination - only if the destination is in the zone.
      via_transit_min  Via from the Quad to a station inside the zone (Garfield Red/Green,
                       59th St Metra), then CTA/Metra the rest of the way.
    Both require the ride to start during service hours (5pm-4am).
    Returns (via_min, via_transit_min, via_leg_min, note). via_leg_min is just the Via ride
    (incl. pickup wait) of the combo; via_transit_min is the whole door-to-door trip."""
    def depart_of(total_min):
        return arrive_local - timedelta(minutes=total_min) if arrive_local else None

    # Daytime arrivals can't use Via (service starts at 5pm): skip the expensive routing.
    if arrive_local is not None and 5.5 <= arrive_local.hour + arrive_local.minute / 60 < VIA_START_HOUR:
        return None, None, None, "Via only runs 5pm-4am"

    direct, combos = None, []
    if in_via_zone(dest):
        car = otp.plan(dest, "car", None)
        if car:
            direct = car[0] / 60 + VIA_WAIT_MIN
    for name, hub in VIA["hubs"].items():
        leg = otp.plan(tuple(hub["dropoff"]), "car", None)          # Via ride to a street point at the station
        onward = otp.plan(dest, "transit", when, origin=tuple(hub["station"]))
        if leg and onward:
            via_leg = leg[0] / 60 + VIA_WAIT_MIN
            combos.append((via_leg + onward[0] / 60, via_leg,
                           f"Via to {name} ({round(via_leg)} min), then {onward[1]}"))
    closed = False
    if direct is not None and not via_open(depart_of(direct)):
        direct, closed = None, True
    open_combos = [c for c in combos if via_open(depart_of(c[0]))]
    closed = closed or (bool(combos) and not open_combos)
    via_transit, via_leg, note = None, None, ""
    if open_combos:
        m, leg_m, n = min(open_combos)
        via_transit, via_leg, note = round(m), round(leg_m), n
    if direct is not None:
        note = "Via direct (destination is inside the Via zone)"
    elif not open_combos:
        note = ("Via only runs 5pm-4am" if closed else
                "Destination is outside the Via zone and no transit connects from a Via stop")
    return (round(direct) if direct is not None else None), via_transit, via_leg, note


# ------------------------------------------------------------------ per-event
def arrival_time(ev):
    """When the traveler wants to arrive: event start (local), or evening of that day if no time."""
    s = ev.get("start") or ""
    if len(s) >= 10:
        day = s[:10]
        hhmm = s[11:16] if len(s) >= 16 and not ev.get("all_day") else DEFAULT_EVENING
        try:
            return datetime.fromisoformat(f"{day}T{hhmm}")
        except ValueError:
            pass
    return None


def minutes(res):
    return round(res[0] / 60) if res else None


def route_event(otp, ev, dest):
    arrive_local = arrival_time(ev)
    when = arrive_local if (arrive_local and arrive_local >= datetime.now()) else None
    walk, bike = otp.plan(dest, "walk", None), otp.plan(dest, "bike", None)
    transit, btransit = otp.plan(dest, "transit", when), otp.plan(dest, "bike_transit", when)
    via_min, via_transit_min, via_leg_min, via_note = via_options(otp, dest, when, arrive_local)
    ev.update(lat=dest[0], lng=dest[1], walk_min=minutes(walk), bike_min=minutes(bike),
              transit_min=minutes(transit), bike_transit_min=minutes(btransit),
              via_min=via_min, via_transit_min=via_transit_min, via_leg_min=via_leg_min, via_note=via_note, transit_route=transit[1] if transit else "")
    pick_best(ev)


def pick_best(ev):
    options = {k: ev[k] for k in ("walk_min", "bike_min", "transit_min", "bike_transit_min", "via_min",
                                  "via_transit_min") if ev.get(k) is not None}
    if options:
        k = min(options, key=options.get)
        ev["best_mode"], ev["best_min"] = k[:-4], options[k]


def add_travel_times(events, otp_base="http://localhost:8080", workers=12):
    otp = OTP(otp_base)
    if not otp.alive():
        print(f"travel: OTP server not reachable at {otp_base}; skipping travel times "
              "(start it with: python otp_server.py serve)", file=sys.stderr)
        return events
    geo = Geocoder()
    todo, campus = [], 0
    for ev in events:                      # geocoding is rate-limited, so do it serially (cached)
        dest = geo.locate(ev)
        if dest:
            todo.append((ev, dest))
        elif ev["source"] == "events.uchicago.edu":
            # campus room/building the map could not find: treat as a short walk from the Quad
            ev.update(walk_min=5, bike_min=3, best_mode="walk", best_min=5,
                      via_note="on campus (estimated)")
            campus += 1
    geo.save()
    done = [0]
    lock = threading.Lock()

    def work(t):
        route_event(otp, *t)
        with lock:
            done[0] += 1
            if done[0] % 100 == 0:  # progress + periodic save so an interrupted run keeps its work
                otp.save()
                print(f"travel: {done[0]}/{len(todo)} events routed", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(workers) as ex:  # OTP queries run in parallel
        list(ex.map(work, todo))
    otp.save()
    print(f"travel: {len(todo)} routed, {campus} assumed on-campus, "
          f"{len(events) - len(todo) - campus} could not be located", file=sys.stderr)
    return events
