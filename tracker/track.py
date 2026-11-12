#!/usr/bin/env python3
"""Daily Pokemon TCG price-drop tracker.

Pulls TCGplayer prices from tcgcsv.com (a free daily mirror), keeps one small
snapshot per day in data/snapshots/, and writes a report of the cards whose
market price has dropped the most:

  report/latest.json   machine-readable report (read by the morning push)
  dashboard/index.html self-contained dashboard page

Standard library only. Run:  python tracker/track.py
"""
import csv
import datetime as dt
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("TCGCSV_BASE", "https://tcgcsv.com").rstrip("/")
SNAP_DIR = ROOT / "data" / "snapshots"
PROD_DIR = ROOT / "data" / "products"
UA = "pkmn-tcg-price-drop/1.0 (personal collection tracker; github.com/kushpatel0703/pkmn-tcg-price-drop)"
PAUSE = float(os.environ.get("TCGCSV_PAUSE", "0.25"))


# ---------------------------------------------------------------- fetching

def fetch_bytes(path, retries=3):
    """GET BASE/path. BASE may be a local folder (used by the tests)."""
    if not BASE.startswith("http"):
        return (Path(BASE) / path).read_bytes()
    url = f"{BASE}/{path}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            time.sleep(PAUSE)
            return data
        except Exception as e:  # network hiccup: back off and retry
            last = e
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


def fetch_json(path):
    return json.loads(fetch_bytes(path))["results"]


# ---------------------------------------------------------------- sets & cards

def tracked_groups(cfg):
    cutoff = cfg["sets_published_on_or_after"]
    excl = [s.lower() for s in cfg.get("exclude_set_names_containing", [])]
    only = [s.lower() for s in cfg.get("only_set_names_containing", [])]
    out = []
    all_groups = fetch_json(f"tcgplayer/{cfg['category_id']}/groups")
    # full list of every set the source has, so the tracked list can be audited
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "all_sets.json").write_text(json.dumps(sorted(
        [{"id": int(g["groupId"]), "name": g.get("name"), "abbr": g.get("abbreviation"),
          "published": (g.get("publishedOn") or "")[:10]} for g in all_groups],
        key=lambda g: (g["published"], g["id"])), ensure_ascii=False, indent=0))
    for g in all_groups:
        name = g.get("name") or ""
        published = (g.get("publishedOn") or "")[:10]
        if not published or published < cutoff:
            continue
        low = name.lower()
        if any(s in low for s in excl):
            continue
        if only and not any(s in low for s in only):
            continue
        out.append({"id": int(g["groupId"]), "name": name, "published": published})
    out.sort(key=lambda g: g["published"])
    return out


def load_products(cfg, group, today):
    """Card details for one set, cached in data/products/ and refreshed weekly
    (daily for sets released in the last 60 days, which still gain cards)."""
    f = PROD_DIR / f"{group['id']}.json"
    if f.exists():
        cached = json.loads(f.read_text())
        age = (today - dt.date.fromisoformat(cached["fetched"])).days
        recent = (today - dt.date.fromisoformat(group["published"])).days < 60
        if age < (1 if recent else 7):
            return cached["cards"]
    cards = {}
    for p in fetch_json(f"tcgplayer/{cfg['category_id']}/{group['id']}/products"):
        ext = {e.get("name"): e.get("value") for e in (p.get("extendedData") or [])}
        if "Number" not in ext:  # sealed product, not a single card
            continue
        cards[str(p["productId"])] = {
            "name": p.get("name") or "",
            "num": ext.get("Number") or "",
            "rarity": ext.get("Rarity") or "",
        }
    PROD_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"fetched": today.isoformat(), "set": group["name"], "cards": cards},
                            ensure_ascii=False, separators=(",", ":")))
    return cards


# ---------------------------------------------------------------- snapshots

def price_rows(raw):
    """tcgcsv price results -> [(productId, printing, market, low)]"""
    rows = []
    for r in raw:
        m = r.get("marketPrice")
        if m is None:
            continue
        rows.append((int(r["productId"]), r.get("subTypeName") or "Normal", float(m), r.get("lowPrice")))
    return rows


def write_snapshot(day, rows):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["productId", "printing", "market", "low"])
    for pid, sub, m, low in sorted(rows):
        w.writerow([pid, sub, f"{m:.2f}", "" if low is None else f"{float(low):.2f}"])
    with open(SNAP_DIR / f"{day}.csv.gz", "wb") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
            gz.write(buf.getvalue().encode())


def read_snapshot(path):
    out = {}
    with gzip.open(path, "rt", newline="") as fh:
        for r in csv.DictReader(fh):
            out[(int(r["productId"]), r["printing"])] = (
                float(r["market"]), float(r["low"]) if r["low"] else None)
    return out


def snapshot_days():
    return sorted(p.name[:10] for p in SNAP_DIR.glob("*.csv.gz"))


def source_date():
    """The day tcgcsv's current prices are for."""
    try:
        txt = fetch_bytes("last-updated.txt").decode().strip()
        return dt.datetime.fromisoformat(txt.replace("Z", "+00:00")).date()
    except Exception:
        return dt.datetime.now(dt.timezone.utc).date()


def backfill(cfg, groups, today, days, limit=25):
    """Fill missing days from tcgcsv's daily price archives, newest first.
    At most `limit` archives per run, and it stops as soon as tcgcsv says
    "too many requests"; the next daily run carries on from there."""
    have = set(snapshot_days())
    ids = {g["id"] for g in groups}
    done_file = ROOT / "data" / "backfill_unavailable.json"
    unavailable = set(json.loads(done_file.read_text())) if done_file.exists() else set()
    missing = [d for d in ((today - dt.timedelta(days=b)).isoformat() for b in range(1, days + 1))
               if d not in have and d not in unavailable]
    if not missing:
        return
    if not shutil.which("7z"):
        print("7z not installed: skipping history backfill")
        return
    print(f"history: {len(missing)} days missing, loading up to {limit}")
    for day in missing[:limit]:
        tmp = Path(tempfile.mkdtemp())
        try:
            arc = tmp / "a.7z"
            arc.write_bytes(fetch_bytes(f"archive/tcgplayer/prices-{day}.ppmd.7z", retries=1))
            subprocess.run(["7z", "x", str(arc), f"-o{tmp}", "-y"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cat = tmp / day / str(cfg["category_id"])
            rows = []
            for gid in ids:
                f = cat / str(gid) / "prices"
                if f.exists():
                    rows += price_rows(json.loads(f.read_text()).get("results", []))
            if rows:
                write_snapshot(day, rows)
                print(f"backfilled {day}: {len(rows)} prices")
            else:
                unavailable.add(day)
            time.sleep(3)  # be polite to tcgcsv
        except Exception as e:
            if "429" in str(e):
                print(f"tcgcsv asked us to slow down at {day}; will continue on the next run")
                break
            print(f"backfill {day} unavailable: {e}")
            unavailable.add(day)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if unavailable:
        done_file.write_text(json.dumps(sorted(unavailable)))


# ---------------------------------------------------------------- analysis

def analyse(cfg, cards, set_of, days):
    """days: sorted list of snapshot dates, last one is 'today'."""
    today = days[-1]
    t = dt.date.fromisoformat(today)
    cur = read_snapshot(SNAP_DIR / f"{today}.csv.gz")
    # "24 hours" compares with the previous day on file, if it is recent (<= 3 days old)
    prev_day = days[-2] if len(days) > 1 else None
    if prev_day and (t - dt.date.fromisoformat(prev_day)).days > 3:
        prev_day = None
    prev = read_snapshot(SNAP_DIR / f"{prev_day}.csv.gz") if prev_day else {}

    # snapshot closest to 7 days back (accept 7-10 days old)
    week_day = None
    for d in reversed(days[:-1]):
        age = (t - dt.date.fromisoformat(d)).days
        if 7 <= age <= 10:
            week_day = d
            break
    week = read_snapshot(SNAP_DIR / f"{week_day}.csv.gz") if week_day else {}

    sums, counts, lows, low_day = {}, {}, {}, {}
    for d in days[:-1]:
        snap = read_snapshot(SNAP_DIR / f"{d}.csv.gz")
        in30 = (t - dt.date.fromisoformat(d)).days <= 30
        for k, (m, _) in snap.items():
            if k not in cur:
                continue
            if in30:
                sums[k] = sums.get(k, 0.0) + m
                counts[k] = counts.get(k, 0) + 1
            if k not in lows or m < lows[k]:
                lows[k], low_day[k] = m, d

    th = cfg["thresholds"]
    history_days = len(days) - 1
    sections = {"day": [], "week": [], "avg30": [], "record": []}

    def row(k, ref, extra=None):
        pid, sub = k
        m, low = cur[k]
        c = cards[str(pid)]
        r = {"id": pid, "name": c["name"], "num": c["num"], "rarity": c["rarity"],
             "set": set_of[str(pid)], "printing": sub, "price": round(m, 2),
             "listing": low, "was": round(ref, 2), "drop": round(ref - m, 2),
             "pct": round((ref - m) / ref * 100, 1),
             # a copy is listed at (or within a few % of) the market price right now
             "buyable": low is not None and float(low) <= m * (1 + cfg.get("buyable_within_pct", 3) / 100)}
        if extra:
            r.update(extra)
        return r

    def passes(m, ref, rule):
        return ref > m and ref - m >= rule["min_drop_usd"] and (ref - m) / ref * 100 >= rule["min_drop_pct"]

    for k, (m, _) in cur.items():
        if str(k[0]) not in cards or m < cfg["min_price"]:
            continue
        if k in prev and passes(m, prev[k][0], th["day"]):
            sections["day"].append(row(k, prev[k][0]))
        if k in week and passes(m, week[k][0], th["week"]):
            sections["week"].append(row(k, week[k][0]))
        if counts.get(k, 0) >= th["avg30"]["min_days_of_history"]:
            avg = sums[k] / counts[k]
            if passes(m, avg, th["avg30"]):
                sections["avg30"].append(row(k, avg))
        if history_days >= th["record"]["min_days_of_history"] and k in lows and passes(m, lows[k], th["record"]):
            sections["record"].append(row(k, lows[k], {"was_on": low_day[k]}))

    for rows in sections.values():
        rows.sort(key=lambda r: (-r["pct"], -r["drop"]))
        del rows[cfg["max_rows_per_section"]:]

    return {
        "date": today,
        "compared": {"day": prev_day, "week": week_day},
        "tracking_since": days[0],
        "days_of_history": len(days),
        "cards_priced": sum(1 for k in cur if str(k[0]) in cards),
        "sets_tracked": len(set(set_of.values())),
        "thresholds": th,
        "min_price": cfg["min_price"],
        "sections": sections,
    }


def write_outputs(report):
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    tpl = (ROOT / "tracker" / "dashboard_template.html").read_text()
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    (ROOT / "dashboard").mkdir(exist_ok=True)
    (ROOT / "dashboard" / "index.html").write_text(tpl.replace("/*__REPORT__*/null", payload))


# ---------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    today = source_date()
    groups = tracked_groups(cfg)
    if not groups:
        sys.exit("no sets matched config.json")
    print(f"{len(groups)} sets tracked; prices dated {today}")

    if cfg.get("backfill_days", 0) > 0:
        backfill(cfg, groups, today, cfg["backfill_days"])

    cards, set_of, rows, failed = {}, {}, [], 0
    for g in groups:
        try:
            gc = load_products(cfg, g, today)
            raw = fetch_json(f"tcgplayer/{cfg['category_id']}/{g['id']}/prices")
        except Exception as e:
            failed += 1
            print(f"skipped {g['name']}: {e}")
            continue
        cards.update(gc)
        for pid in gc:
            set_of[pid] = g["name"]
        rows += [r for r in price_rows(raw) if str(r[0]) in gc]
    if failed > len(groups) / 2 or not rows:
        sys.exit("too many sets failed to load; not writing a report today")

    write_snapshot(today.isoformat(), rows)
    report = analyse(cfg, cards, set_of, snapshot_days())
    write_outputs(report)
    s = report["sections"]
    print(f"{report['cards_priced']} prices | 24h {len(s['day'])} | 7d {len(s['week'])} | "
          f"30d avg {len(s['avg30'])} | record lows {len(s['record'])}")


if __name__ == "__main__":
    main()
