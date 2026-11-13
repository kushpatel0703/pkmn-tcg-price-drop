#!/usr/bin/env python3
"""Daily Pokemon TCG price tracker.

Pulls TCGplayer prices from tcgcsv.com (a free community mirror), keeps one
snapshot per day in data/snapshots/, and writes:

  report/latest.json   machine-readable report (read by the morning push)
  dashboard/index.html self-contained dashboard page

Order of work in a run:
  1. fetch today's prices, check them, save today's snapshot
  2. load a few missing history days from tcgcsv's archives (scheduled runs only)
  3. analyse the history and write the report

The run fails (non-zero exit, nothing written for today) rather than save
data that looks partial or wrong. A day with no snapshot is filled in later
from the archives, so failing is safe; saving bad data is not.

Standard library only. Run:  python tracker/track.py
Environment: SKIP_BACKFILL=1 skips archive downloads (used on push-triggered runs).
"""
import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import score_card, supply_signal, median  # noqa: E402
import pricecheck  # noqa: E402
import nearmint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("TCGCSV_BASE", "https://tcgcsv.com").rstrip("/")
SNAP_DIR = ROOT / "data" / "snapshots"
PROD_DIR = ROOT / "data" / "products"
STATE_FILE = ROOT / "data" / "history_state.json"
UA = "pkmn-tcg-price-drop/1.1 (personal collection tracker; github.com/kushpatel0703/pkmn-tcg-price-drop)"
PAUSE = float(os.environ.get("TCGCSV_PAUSE", "0.25"))
ARCHIVE_PAUSE = float(os.environ.get("TCGCSV_ARCHIVE_PAUSE", "10"))
MAX_JSON_BYTES = 50 * 2**20
MAX_ARCHIVE_BYTES = 400 * 2**20
NAN = float("nan")


# ---------------------------------------------------------------- fetching

class FetchError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


STOPPED = False   # set once tcgcsv says "too many requests" or "forbidden": no more requests this run


def fetch_bytes(path, retries=3, max_bytes=MAX_JSON_BYTES):
    """GET BASE/path. BASE may be a local folder (used by the tests).
    4xx answers are not retried; 429 and 403 stop all further requests."""
    global STOPPED
    if STOPPED:
        raise FetchError("not requesting: tcgcsv asked us to stop for this run", 429)
    if not BASE.startswith("http"):
        f = Path(BASE) / path
        if not f.exists():
            raise FetchError(f"not found: {path}", 404)
        return f.read_bytes()
    url = f"{BASE}/{path}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                size = int(r.headers.get("Content-Length") or 0)
                if size > max_bytes:
                    raise FetchError(f"{url} is too large ({size} bytes)")
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise FetchError(f"{url} is too large")
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                STOPPED = True
                raise FetchError(f"tcgcsv answered {e.code} for {url}; stopping requests for this run", e.code)
            if 400 <= e.code < 500:
                raise FetchError(f"{e.code} for {url}", e.code)
            last = e
        except FetchError:
            raise
        except Exception as e:  # network hiccup or 5xx: back off and retry
            last = e
        time.sleep(5 * (attempt + 1))
    raise FetchError(f"could not fetch {url}: {last}")


def fetch_json(path):
    try:
        data = json.loads(fetch_bytes(path))
    except ValueError as e:
        raise FetchError(f"bad JSON from {path}: {e}")
    if not isinstance(data, dict) or data.get("success") is False or not isinstance(data.get("results"), list):
        raise FetchError(f"unsuccessful response from {path}: {str(data)[:200]}")
    return data["results"]


def clean(s, limit=120):
    """Upstream names are untrusted: keep printable characters, cap the length."""
    s = "".join(ch for ch in str(s if s is not None else "") if ch.isprintable())
    return s.strip()[:limit]


# ---------------------------------------------------------------- sets & cards

def era_of(name, published):
    if name.startswith("SWSH"):
        return "Sword & Shield"
    if name.startswith("SV"):
        return "Scarlet & Violet"
    if name.startswith("ME"):
        return "Mega Evolution"
    if published < "2023-03-31":
        return "Sword & Shield"
    if published < "2025-09-26":
        return "Scarlet & Violet"
    return "Mega Evolution"


def tracked_groups(cfg):
    cutoff = cfg["sets_published_on_or_after"]
    excl = [s.lower() for s in cfg.get("exclude_set_names_containing", [])]
    only = [s.lower() for s in cfg.get("only_set_names_containing", [])]
    all_groups = fetch_json(f"tcgplayer/{cfg['category_id']}/groups")
    if len(all_groups) < 50:
        raise FetchError(f"only {len(all_groups)} sets listed upstream; looks like a partial response")
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "all_sets.json").write_text(json.dumps(sorted(
        [{"id": int(g["groupId"]), "name": clean(g.get("name")), "abbr": clean(g.get("abbreviation"), 20),
          "published": clean(g.get("publishedOn"), 10)} for g in all_groups],
        key=lambda g: (g["published"], g["id"])), ensure_ascii=False, indent=0))
    include = set(cfg.get("include_set_ids", []))
    exclude = set(cfg.get("exclude_set_ids", []))
    fixed_dates = cfg.get("set_dates", {})
    out = []
    for g in all_groups:
        name = clean(g.get("name"))
        gid = int(g["groupId"])
        published = fixed_dates.get(str(gid)) or clean(g.get("publishedOn"), 10)
        try:
            dt.date.fromisoformat(published)
        except ValueError:
            published = ""
        if gid in exclude:
            continue
        if gid not in include and (not published or published < cutoff or gid < cfg.get("min_set_id", 0)):
            continue
        if not published:
            published = cutoff
        low = name.lower()
        if any(s in low for s in excl):
            continue
        if only and not any(s in low for s in only):
            continue
        out.append({"id": gid, "name": name, "published": published, "era": era_of(name, published)})
    out.sort(key=lambda g: (g["published"], g["id"]))
    return out


def load_products(cfg, group, today):
    """Card and sealed-product names for one set, cached in data/products/.
    Refreshed daily for sets released in the last 60 days, otherwise about
    weekly (staggered by set so they do not all refresh on the same day).
    An empty or much smaller list than the cache is ignored: the cache stays."""
    f = PROD_DIR / f"{group['id']}.json"
    cached = json.loads(f.read_text()) if f.exists() else None
    if cached and "sealed" in cached:
        age = (today - dt.date.fromisoformat(cached["fetched"])).days
        recent = (today - dt.date.fromisoformat(group["published"])).days < 60
        due = age >= 1 if recent else (age >= 7 and (today.toordinal() + group["id"]) % 7 == 0) or age >= 14
        if not due:
            return cached["cards"], cached["sealed"]
    cards, sealed = {}, {}
    for p in fetch_json(f"tcgplayer/{cfg['category_id']}/{group['id']}/products"):
        pid = str(int(p["productId"]))
        ext = {e.get("name"): e.get("value") for e in (p.get("extendedData") or [])}
        if "Number" not in ext:  # sealed product, not a single card
            sealed[pid] = clean(p.get("name"))
            continue
        cards[pid] = {"name": clean(p.get("name")), "num": clean(ext.get("Number"), 20),
                      "rarity": clean(ext.get("Rarity"), 40)}
    if cached and "sealed" in cached:
        before = len(cached["cards"]) + len(cached["sealed"])
        if len(cards) + len(sealed) < 0.8 * before:
            print(f"{group['name']}: product list shrank from {before} to {len(cards) + len(sealed)}; keeping the cached list")
            return cached["cards"], cached["sealed"]
    PROD_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps({"fetched": today.isoformat(), "set": group["name"], "cards": cards, "sealed": sealed},
                               ensure_ascii=False, separators=(",", ":")).encode())
    return cards, sealed


# ---------------------------------------------------------------- snapshots

def price_rows(raw):
    """tcgcsv price results -> [(productId, printing, market, low)].
    Drops missing, non-finite or non-positive prices; a cheapest listing more
    than 20x the market price is treated as junk."""
    rows = []
    for r in raw:
        try:
            m = float(r.get("marketPrice"))
            pid = int(r["productId"])
        except (TypeError, ValueError, KeyError):
            continue
        if not math.isfinite(m) or m <= 0:
            continue
        try:
            low = float(r.get("lowPrice"))
            if not math.isfinite(low) or low <= 0 or low > 20 * m:
                low = None
        except (TypeError, ValueError):
            low = None
        rows.append((pid, clean(r.get("subTypeName"), 40) or "Normal", m, low))
    return rows


def atomic_write(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_snapshot(day, rows, prefer="new"):
    """Save rows for a day. If the day already has a file, the two are merged
    (prefer="new": new values win; prefer="old": existing values win), so a
    second run can only add rows, never lose them. Skips the write when the
    content would not change."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"{day}.csv.gz"
    new = {(pid, sub): (round(m, 2), None if low is None else round(float(low), 2)) for pid, sub, m, low in rows}
    old = read_snapshot(path) if path.exists() else {}
    merged = {**old, **new} if prefer == "new" else {**new, **old}
    if old and merged == old:
        return len(old)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["productId", "printing", "market", "low"])
    for (pid, sub), (m, low) in sorted(merged.items()):
        w.writerow([pid, sub, f"{m:.2f}", "" if low is None else f"{low:.2f}"])
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue().encode())
    atomic_write(path, out.getvalue())
    return len(merged)


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
    """The day tcgcsv's current prices are for. Fails rather than guess."""
    txt = fetch_bytes("last-updated.txt").decode(errors="replace").strip()
    try:
        return dt.datetime.fromisoformat(txt.replace("Z", "+00:00")).date()
    except ValueError:
        raise FetchError(f"cannot read tcgcsv's last-updated.txt: {txt[:80]!r}")


# ---------------------------------------------------------------- history from archives

def load_state():
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        return set(s.get("unavailable_days", [])), {d: set(v) for d, v in s.get("checked", {}).items()}
    return set(), {}


def save_state(unavailable, checked):
    atomic_write(STATE_FILE, json.dumps({
        "unavailable_days": sorted(unavailable),
        "checked": {d: sorted(v) for d, v in sorted(checked.items())},
        "_about": "unavailable_days: archives tcgcsv does not have (HTTP 404). checked: per day, "
                  "set ids already looked for in that day's archive, so they are not downloaded again."},
        indent=0).encode())


def archive_rows(cfg, day, group_ids):
    """Download one day's archive and return {group_id: rows} for the groups present."""
    tmp = Path(tempfile.mkdtemp(prefix="tcgcsv-"))
    try:
        arc = tmp / "a.7z"
        arc.write_bytes(fetch_bytes(f"archive/tcgplayer/prices-{day}.ppmd.7z", retries=2,
                                    max_bytes=MAX_ARCHIVE_BYTES))
        out = tmp / "x"
        try:
            subprocess.run(["7z", "x", str(arc), f"-o{out}", "-y"], check=True, timeout=900,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise FetchError(f"could not unpack the {day} archive: {e}")
        base = (out / day / str(cfg["category_id"])).resolve()
        found = {}
        for gid in group_ids:
            f = base / str(gid) / "prices"
            # only read regular files that really sit inside the unpacked folder
            if f.is_file() and not f.is_symlink() and f.resolve().is_relative_to(out.resolve()):
                found[gid] = price_rows(json.loads(f.read_text()).get("results", []))
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def history(cfg, groups, today, set_cards):
    """Fill gaps in the price history from tcgcsv's archives, a few per run:
      - days with no snapshot at all (up to backfill_days back)
      - days whose snapshot has no card prices for a set we track now (sets
        added later, or a day saved while a set was failing upstream)
    Each archive is downloaded at most once for a given set. Only HTTP 404
    marks a day as unavailable; any other failure stops and retries next run."""
    if os.environ.get("SKIP_BACKFILL"):
        print("history: skipped on this run")
        return
    if cfg.get("backfill_days", 0) <= 0:
        return
    if not shutil.which("7z"):
        print("history: 7z not installed, skipping")
        return
    unavailable, checked = load_state()
    have = snapshot_days()
    have_set = set(have)
    t = today
    limit = int(os.environ.get("ARCHIVES_PER_RUN") or cfg.get("archives_per_run", 10))
    work = []   # (day, kind, group_ids)
    start = cfg.get("history_start", "2024-02-08")   # tcgcsv's archives begin here
    for b in range(1, cfg["backfill_days"] + 1):
        d = (t - dt.timedelta(days=b)).isoformat()
        if d < start:
            break
        if d not in have_set and d not in unavailable:
            work.append((d, "missing", [g["id"] for g in groups]))
    # sets with prices today but none on an earlier day they already existed
    today_snap = read_snapshot(SNAP_DIR / f"{t.isoformat()}.csv.gz")
    today_pids = {k[0] for k in today_snap}
    live = [g for g in groups if len(set_cards.get(g["id"], set()) & today_pids) >= 5]
    # which live sets have card prices on each earlier day
    present = {}
    for d in have:
        if d >= t.isoformat() or d in unavailable:
            continue
        snap_pids = {k[0] for k in read_snapshot(SNAP_DIR / f"{d}.csv.gz")}
        present[d] = {g["id"] for g in live if set_cards.get(g["id"], set()) & snap_pids}
    first_seen = {}
    for d in sorted(present):
        for gid in present[d]:
            first_seen.setdefault(gid, d)
    # a gap is a day missing a set that had prices on an earlier day (a hole), not the
    # stretch before the source started pricing it
    for d in sorted(present, reverse=True):
        gaps = [g["id"] for g in live
                if g["id"] not in present[d] and first_seen.get(g["id"], "9999") < d
                and g["id"] not in checked.get(d, set())]
        if gaps:
            work.append((d, "gaps", gaps))
    if not work:
        return
    work.sort(key=lambda w: w[0], reverse=True)
    print(f"history: {sum(w[1] == 'missing' for w in work)} missing days, "
          f"{sum(w[1] == 'gaps' for w in work)} days with missing sets; loading up to {limit}")
    for i, (day, kind, gids) in enumerate(work[:limit]):
        if i:
            time.sleep(ARCHIVE_PAUSE)  # be gentle with tcgcsv
        try:
            found = archive_rows(cfg, day, gids)
        except FetchError as e:
            if e.status == 404:
                print(f"history: no archive for {day}")
                if kind == "missing":
                    unavailable.add(day)
                else:
                    checked.setdefault(day, set()).update(gids)
                continue
            print(f"history: stopping at {day}: {e}")
            if e.status in (403, 429):
                print("history: tcgcsv asked us to slow down")
            break
        rows = [r for gid in gids for r in found.get(gid, [])]
        if kind == "missing":
            if rows:
                write_snapshot(day, rows, prefer="old")
                print(f"history: loaded {day} ({len(rows)} prices)")
            else:
                unavailable.add(day)
        else:
            if rows:
                write_snapshot(day, rows, prefer="old")
            checked.setdefault(day, set()).update(gids)
            print(f"history: {day}: added {len(rows)} prices for {len(found)} of {len(gids)} missing sets")
    save_state(unavailable, checked)


# ---------------------------------------------------------------- checks on today's data

def check_today(rows, prev_snap, set_products, groups):
    """Refuse data that looks partial or implausible compared with the last
    good day. Returns a list of problems (empty when fine)."""
    problems = []
    if not prev_snap:
        return problems
    new_pids = {pid for pid, _, _, _ in rows}
    tracked = set().union(*set_products.values()) if set_products else set()
    new_tracked = {(pid, sub) for pid, sub, _, _ in rows if pid in tracked}
    prev_tracked = {k for k in prev_snap if k[0] in tracked}
    if prev_tracked and len(new_tracked) < 0.95 * len(prev_tracked):
        problems.append(f"only {len(new_tracked)} prices today vs {len(prev_tracked)} on the last day on file")
    for g in groups:
        pids = set_products.get(g["id"], set())
        before = {k[0] for k in prev_snap if k[0] in pids}
        now = pids & new_pids
        if len(before) >= 20 and len(now) < 0.8 * len(before):
            problems.append(f"{g['name']}: {len(now)} products priced today vs {len(before)} on the last day on file")
    changes = [m / prev_snap[(pid, sub)][0] - 1 for pid, sub, m, _ in rows
               if (pid, sub) in prev_snap and prev_snap[(pid, sub)][0] >= 1]
    if len(changes) > 500:
        mid = median(changes)
        moved = sum(abs(c) > 0.3 for c in changes) / len(changes)
        if abs(mid) > 0.10 or moved > 0.25:
            problems.append(f"prices look implausible: median change {mid:+.0%}, {moved:.0%} of cards moved over 30%")
    return problems


# ---------------------------------------------------------------- analysis

CAVEATS = [
    "Market price is TCGplayer's recent-sales estimate for the printing, not split by condition; "
    "treat it as the Near Mint price.",
    "Cheapest listing is for any condition and excludes shipping; it is usually a played copy.",
    "Labels describe price history, not a prediction. In the Aug-Nov 2026 backtest every label, "
    "including Stable price, was cheaper on average a month later in a falling market; "
    "the Wait labels fell the most.",
]


def load_matrix(days, keep):
    """Market price of each kept (product, printing) on every day: {key: array},
    NaN where the day has no price. Reads each snapshot once."""
    n = len(days)
    matrix = {k: array("f", [NAN]) * n for k in keep}
    for i, d in enumerate(days):
        with gzip.open(SNAP_DIR / f"{d}.csv.gz", "rt", newline="") as fh:
            rd = csv.reader(fh)
            next(rd, None)
            for pid, sub, m, _ in rd:
                a = matrix.get((int(pid), sub))
                if a is not None:
                    a[i] = float(m)
    return matrix


def analyse(cfg, cards, set_of, days, groups, sealed_by_set=None):
    """days: sorted list of snapshot dates, last one is 'today'."""
    today = days[-1]
    t = dt.date.fromisoformat(today)
    n = len(days)
    ords = [dt.date.fromisoformat(d).toordinal() for d in days]
    cur = read_snapshot(SNAP_DIR / f"{today}.csv.gz")
    sealed_ids = {pid for s in (sealed_by_set or {}).values() for pid in s}
    keep = {k for k, (m, _) in cur.items()
            if (str(k[0]) in cards and m >= cfg["min_price"]) or str(k[0]) in sealed_ids}
    matrix = load_matrix(days, keep)

    prev_i = n - 2 if n > 1 and ords[-1] - ords[-2] <= 3 else None
    prev_day = days[prev_i] if prev_i is not None else None
    week_i = next((i for i in range(n - 2, -1, -1) if 7 <= ords[-1] - ords[i] <= 10), None)
    week_day = days[week_i] if week_i is not None else None

    sums, counts, lows, low_day, series, first_day, ndays, prev, week = {}, {}, {}, {}, {}, {}, {}, {}, {}
    for k, a in matrix.items():
        for i in range(n - 1):
            m = a[i]
            if math.isnan(m):
                continue
            age = ords[-1] - ords[i]
            if k not in first_day:
                first_day[k] = days[i]
            ndays[k] = ndays.get(k, 0) + 1
            if age <= 93:
                series.setdefault(k, []).append((age, m))
            if age <= 30:
                sums[k] = sums.get(k, 0.0) + m
                counts[k] = counts.get(k, 0) + 1
            if k not in lows or m < lows[k]:
                lows[k], low_day[k] = m, days[i]
        if prev_i is not None and not math.isnan(a[prev_i]):
            prev[k] = a[prev_i]
        if week_i is not None and not math.isnan(a[week_i]):
            week[k] = a[week_i]

    th = cfg["thresholds"]
    group_of = {g["name"]: g for g in groups}
    undated_ids = set(cfg.get("undated_set_ids", []))
    supply = {}
    for name, sealed in (sealed_by_set or {}).items():
        items = [(sealed[str(k[0])], m, series.get(k, [])) for k, (m, _) in cur.items() if str(k[0]) in sealed]
        sig = supply_signal(items)
        if sig:
            supply[name] = sig

    def card_age(k):
        if k not in first_day:
            return 0
        if first_day[k] == days[0]:
            return None                   # seen since the history starts: age unknown, not new
        return (t - dt.date.fromisoformat(first_day[k])).days

    card_keys = [k for k in matrix if str(k[0]) in cards]
    scores = {}
    for k in card_keys:
        m = cur[k][0]
        pid = str(k[0])
        g = group_of.get(set_of[pid])
        months = (t - dt.date.fromisoformat(g["published"])).days / 30.44 if g else None
        sc = score_card(m, series.get(k, []), months, bool(g and g["id"] in undated_ids),
                        card_age(k), supply.get(set_of[pid]))
        if sc:
            scores[k] = sc

    def label_of(k):
        sc = scores.get(k)
        if sc:
            return sc["label"], sc["score"], sc["why"]
        g = group_of.get(set_of[str(k[0])])
        age = card_age(k)
        if (g and g["id"] not in undated_ids and (t - dt.date.fromisoformat(g["published"])).days < 91) \
                or (age is not None and age < 45):
            return "Wait, too new", None, ["new release; prices usually keep falling for a few months"]
        return None, None, [f"only {ndays.get(k, 0) + 1} days of price history, too few to judge"]

    def row(k, ref, extra=None):
        pid, sub = k
        m, low = cur[k]
        c = cards[str(pid)]
        r = {"id": pid, "name": c["name"], "num": c["num"], "rarity": c["rarity"],
             "set": set_of[str(pid)], "printing": sub, "price": round(m, 2),
             "listing": low, "was": round(ref, 2), "drop": round(ref - m, 2),
             "pct": round((ref - m) / ref * 100, 1), "hist_days": ndays.get(k, 0) + 1}
        label, score, why = label_of(k)
        if label:
            r["label"] = label
        if score is not None:
            r["score"] = score
        r["why"] = why
        if extra:
            r.update(extra)
        return r

    def passes(m, ref, rule):
        return ref > m and ref - m >= rule["min_drop_usd"] and (ref - m) / ref * 100 >= rule["min_drop_pct"]

    sections = {"stable": [], "day": [], "week": [], "avg30": [], "record": []}
    for k in card_keys:
        m = cur[k][0]
        if k in prev and passes(m, prev[k], th["day"]):
            sections["day"].append(row(k, prev[k]))
        if k in week and passes(m, week[k], th["week"]):
            sections["week"].append(row(k, week[k]))
        if counts.get(k, 0) >= th["avg30"]["min_days_of_history"]:
            avg = sums[k] / counts[k]
            if passes(m, avg, th["avg30"]):
                sections["avg30"].append(row(k, avg))
        if ndays.get(k, 0) >= th["record"]["min_days_of_history"] and k in lows and passes(m, lows[k], th["record"]):
            sections["record"].append(row(k, lows[k], {"was_on": low_day[k], "since": first_day[k]}))

    labels = {}
    for k, sc in scores.items():
        labels[sc["label"]] = labels.get(sc["label"], 0) + 1
        if sc["label"] == "Stable price":
            sections["stable"].append(row(k, sc["avg90"]))

    totals = {key: len(rows) for key, rows in sections.items()}
    cap = cfg.get("max_rows_per_set", 40)
    for key, rows in sections.items():
        if key == "stable":
            rows.sort(key=lambda r: (-r["price"], r["set"]))
            continue                      # every stable card is shipped, for every set
        rows.sort(key=lambda r: (-r["pct"], -r["drop"]))
        per_set, kept = {}, []
        for r in rows:                    # cap per set so one set cannot crowd out the others
            per_set[r["set"]] = per_set.get(r["set"], 0) + 1
            if per_set[r["set"]] <= cap:
                kept.append(r)
        rows[:] = kept

    # ---- price check: where today's price sits in each card's own last year
    pc_cards, checks = [], {}
    for k in card_keys:
        c = pricecheck.check(matrix[k], ords, n - 1)
        if not c:
            continue
        checks[k] = c
        y = pricecheck.check(matrix[k], ords, n - 2) if prev_i is not None else None
        pid, sub = k
        cd = cards[str(pid)]
        label, score, _ = label_of(k)
        pc_cards.append({
            "id": pid, "name": cd["name"], "num": cd["num"], "rarity": cd["rarity"], "set": set_of[str(pid)],
            "printing": sub, "price": round(c["price"], 2), "listing": cur[k][1],
            "low": round(c["low"], 2), "high": round(c["high"], 2), "typical": round(c["med"], 2),
            "target": round(c["target"], 2), "days": c["days"], "at": c["at"], "flat": c["flat"],
            "newly": bool(c["at"] and y and not y["at"]),
            "above_low": round((c["price"] / c["low"] - 1) * 100, 1),
            "label": label, "score": score, "spark": pricecheck.spark(matrix[k], ords, n - 1)})
    pc_cards.sort(key=lambda r: (r["above_low"], -r["price"]))
    card_matrix = {k: matrix[k] for k in card_keys}
    card_score = pricecheck.scorecard(card_matrix, ords, days, cfg["min_price"],
                                      ROOT / "data" / "pricecheck_scorecard.json",
                                      cfg.get("pricecheck_rule_since", today))

    # ---- whole-set view: cost of every printing now vs at each card's good price
    setview = {}
    for k, (m, _) in cur.items():
        pid = str(k[0])
        if pid not in cards:
            continue
        sv = setview.setdefault(set_of[pid], {"printings": 0, "market_total": 0.0, "target_total": 0.0,
                                               "checked": 0, "at": 0, "above": 0})
        sv["printings"] += 1
        sv["market_total"] += m
        c = checks.get(k)
        if c:
            sv["checked"] += 1
            sv["at" if c["at"] else "above"] += 1
            sv["target_total"] += min(m, c["target"])
        else:
            sv["target_total"] += m
    for sv in setview.values():
        sv["market_total"] = round(sv["market_total"], 2)
        sv["target_total"] = round(sv["target_total"], 2)

    in_report = set(set_of.values())
    return {
        "date": today,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "compared": {"day": prev_day, "week": week_day},
        "history_from": days[0],
        "days_of_history": len(days),
        "cards_priced": sum(1 for k in cur if str(k[0]) in cards),
        "cards_over_min": len(card_keys),
        "sets_tracked": len(in_report),
        "sets": [{"name": g["name"], "published": g["published"], "era": g["era"],
                  **({"supply": supply[g["name"]]} if g["name"] in supply else {}),
                  **({"view": setview[g["name"]]} if g["name"] in setview else {})}
                 for g in groups if g["name"] in in_report],
        "thresholds": th,
        "min_price": cfg["min_price"],
        "max_rows_per_set": cap,
        "scored": len(scores),
        "labels": labels,
        "totals": totals,
        "caveats": CAVEATS,
        "pricecheck": {"rule": {"window_days": pricecheck.WINDOW_DAYS, "target_pctile": int(pricecheck.TARGET_PCTILE * 100),
                                "min_days": pricecheck.MIN_DAYS, "spark_step_days": pricecheck.SPARK_STEP},
                       "checked": len(pc_cards), "at_target": sum(r["at"] for r in pc_cards),
                       "newly_at_target": sum(r["newly"] for r in pc_cards),
                       "scorecard": card_score, "cards": pc_cards},
        "sections": sections,
    }


def write_outputs(report):
    (ROOT / "report").mkdir(exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    atomic_write(ROOT / "report" / "latest.json", text.encode())
    tpl = (ROOT / "tracker" / "dashboard_template.html").read_text()
    # inside <script>, escape characters that could end or confuse the script element
    payload = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
                  .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (ROOT / "dashboard").mkdir(exist_ok=True)
    atomic_write(ROOT / "dashboard" / "index.html", tpl.replace("/*__REPORT__*/null", payload).encode())


# ---------------------------------------------------------------- main

def history_only():
    """Load missing history from the archives without fetching today's prices
    (used by the one-time history workflow). Uses the cached product lists."""
    cfg = json.loads((ROOT / "config.json").read_text())
    days = snapshot_days()
    if not days:
        sys.exit("no snapshots yet; run the daily job first")
    try:
        groups = tracked_groups(cfg)
    except FetchError as e:
        sys.exit(f"FAILED: {e}")
    set_cards = {}
    for g in groups:
        f = PROD_DIR / f"{g['id']}.json"
        if f.exists():
            set_cards[g["id"]] = {int(p) for p in json.loads(f.read_text())["cards"]}
    history(cfg, groups, dt.date.fromisoformat(days[-1]), set_cards)
    print(f"history now covers {len(snapshot_days())} days, from {snapshot_days()[0]}")


def main():
    # read the Near Mint API key once and keep it out of the environment of later steps
    nm_key = os.environ.pop("JUSTTCG_API_KEY", "")
    if "--history-only" in sys.argv:
        return history_only()
    cfg = json.loads((ROOT / "config.json").read_text())
    try:
        today = source_date()
        groups = tracked_groups(cfg)
    except FetchError as e:
        sys.exit(f"FAILED: {e}")
    utc_today = dt.datetime.now(dt.timezone.utc).date()
    if (utc_today - today).days > 1:
        sys.exit(f"FAILED: tcgcsv has not updated since {today}; not writing a report")
    print(f"{len(groups)} sets tracked; prices dated {today}")

    cards, set_of, rows, failed, sealed_by_set, set_products, set_cards = {}, {}, [], [], {}, {}, {}
    for g in groups:
        try:
            gc, gs = load_products(cfg, g, today)
            raw = fetch_json(f"tcgplayer/{cfg['category_id']}/{g['id']}/prices")
        except FetchError as e:
            failed.append(g["name"])
            print(f"failed {g['name']}: {e}")
            if STOPPED:
                break
            continue
        cards.update(gc)
        for pid in gc:
            set_of[pid] = g["name"]
        sealed_by_set[g["name"]] = gs
        set_products[g["id"]] = {int(p) for p in list(gc) + list(gs)}
        set_cards[g["id"]] = {int(p) for p in gc}
        # keep every price in the set (also cards added since the product cache was
        # refreshed), the same rows the archive path keeps; analysis uses what it knows
        rows += price_rows(raw)
    if failed:
        sys.exit(f"FAILED: {len(failed)} of {len(groups)} sets could not be loaded "
                 f"({', '.join(failed[:5])}{'...' if len(failed) > 5 else ''}); not writing today's snapshot")

    earlier = [d for d in snapshot_days() if d < today.isoformat()]
    prev_snap = read_snapshot(SNAP_DIR / f"{earlier[-1]}.csv.gz") if earlier else {}
    problems = check_today(rows, prev_snap, set_products, groups)
    if problems:
        sys.exit("FAILED: today's data looks wrong; not saving it:\n  " + "\n  ".join(problems[:10]))

    n = write_snapshot(today.isoformat(), rows, prefer="new")
    print(f"saved {today}: {n} prices")
    keep = {str(g["id"]) for g in groups}
    for f in PROD_DIR.glob("*.json"):
        if f.stem not in keep:
            f.unlink()

    history(cfg, groups, today, set_cards)

    report = analyse(cfg, cards, set_of, snapshot_days(), groups, sealed_by_set)
    pc = report["pricecheck"]
    nm_path = ROOT / "data" / "nearmint.json"
    nearmint.refresh(nm_path, pc["cards"], report["date"], nm_key)
    pc["nm_cards"] = nearmint.annotate(nm_path, pc["cards"], report["date"])
    pc["nm_at_target"] = sum(1 for c in pc["cards"] if c.get("nm_at"))
    write_outputs(report)
    s, lab = report["sections"], report["labels"]
    print(f"{report['cards_priced']} prices | stable {len(s['stable'])} | 24h {len(s['day'])} | 7d {len(s['week'])} | "
          f"30d avg {len(s['avg30'])} | record lows {len(s['record'])} | labels {lab}")


if __name__ == "__main__":
    main()
