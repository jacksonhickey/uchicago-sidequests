#!/usr/bin/env python3
"""Download data for, build, and run a local OpenTripPlanner (OTP) server for Chicago.

    python otp_server.py setup            # download OTP jar + CTA/Metra schedules + Chicago street map
    python otp_server.py serve            # build the routing graph (first time) and serve on :8080
    python otp_server.py serve --rebuild  # force a graph rebuild (e.g. after refreshing schedules)
    python otp_server.py setup --refresh  # re-download the schedules/map

Requires Java 21+ (https://adoptium.net). Needs ~4 GB free RAM while building.
Everything is stored in ./otp/ (git-ignored).
"""
import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OTP_VERSION = "2.8.1"
OTP_DIR = Path(__file__).parent / "otp"
FILES = {
    f"otp-shaded-{OTP_VERSION}.jar": f"https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/{OTP_VERSION}/otp-shaded-{OTP_VERSION}.jar",
    "cta-gtfs.zip": "https://www.transitchicago.com/downloads/sch_data/google_transit.zip",
    "metra-gtfs.zip": "https://schedules.metrarail.com/gtfs/schedule.zip",
    "chicago.osm.pbf": "https://download.bbbike.org/osm/bbbike/Chicago/Chicago.osm.pbf",
}
JAR = OTP_DIR / f"otp-shaded-{OTP_VERSION}.jar"
DATA_FILES = ["cta-gtfs.zip", "metra-gtfs.zip", "chicago.osm.pbf"]


def download(name, url, refresh):
    dest = OTP_DIR / name
    if dest.exists() and not (refresh and name != JAR.name):
        print(f"have {name}")
        return
    print(f"downloading {name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "uchicago-sidequests/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest.with_suffix(dest.suffix + ".part"), "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    dest.with_suffix(dest.suffix + ".part").replace(dest)


def setup(refresh=False):
    OTP_DIR.mkdir(exist_ok=True)
    for name, url in FILES.items():
        download(name, url, refresh)
    graph = OTP_DIR / "graph.obj"
    if refresh and graph.exists():
        graph.unlink()  # schedules changed -> force rebuild


def serve(rebuild=False, memory="4G"):
    setup()
    graph = OTP_DIR / "graph.obj"
    if rebuild and graph.exists():
        graph.unlink()
    mode = ["--load"] if graph.exists() else ["--build", "--save", "--serve"]
    cmd = ["java", f"-Xmx{memory}", "-jar", str(JAR), *mode, str(OTP_DIR)]
    print(" ".join(cmd))
    sys.exit(subprocess.call(cmd))


def wait_until_ready(url="http://localhost:8080/otp/gtfs/v1", timeout=1200):
    """Used by CI: block until the server answers."""
    import requests
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.post(url, json={"query": "{__typename}"}, timeout=5).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "serve", "wait"])
    ap.add_argument("--refresh", action="store_true", help="re-download schedules/map")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the routing graph")
    ap.add_argument("--memory", default="4G")
    a = ap.parse_args()
    if a.command == "setup":
        setup(a.refresh)
    elif a.command == "serve":
        serve(a.rebuild, a.memory)
    else:
        sys.exit(0 if wait_until_ready() else 1)
