# UChicago Sidequests

Events from [events.uchicago.edu](https://events.uchicago.edu/) and [do312.com](https://do312.com/events),
de-duplicated, with price, age limits, categories, photos, and estimated travel time from the Main Quad
(walk / bike / CTA+Metra / RideSmart by Via).

- `index.html` – the website (static; reads `events.json`). Browse by day, filter by category/time/price/travel.
- `scrape_events.py` – scrapes both sites into `events.json` / `events.csv` (de-duplicated via `events.db`).
- `travel.py` – travel-time estimates using a local OpenTripPlanner server.
- `otp_server.py` – downloads CTA/Metra schedules + Chicago map and runs OpenTripPlanner.
- `via_zone.json` – RideSmart by Via service area, stations, and hours (5pm–4am, UChicago students only).
- `.github/workflows/events.yml` – refreshes everything daily and commits the results.

## Run locally

Requires Python 3.10+ (`pip install requests`) and Java 21+ (for travel times).

```bash
python otp_server.py serve     # terminal 1: first start builds the routing graph (~6 min)
python scrape_events.py        # terminal 2: adds travel times automatically if the server is up
python -m http.server 8000     # then open http://localhost:8000
```

Options: `--days N` (UChicago window), `--travel off`, `--no-details` (faster Do312 scrape).
On Windows, use `py` if `python` opens the Microsoft Store.

## Deploy

Push to GitHub, then enable **Settings → Pages → Deploy from branch → `main` / root**.
The scheduled workflow (Actions tab → *Scrape events + travel times*) updates `events.json` daily.

Travel times are estimates from scheduled service (no live delays). Via times assume an 8‑minute pickup wait.
