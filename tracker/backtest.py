#!/usr/bin/env python3
"""Backtest the labels on the price history in data/snapshots/.

For a series of past dates it labels every card using only what was known on
that date, then asks the collector's question: would waiting have been
cheaper? For each label it reports, over the next N days:

  median change   market price at day N vs the day it was labelled
  cheaper 5%+     share of cards at least 5% cheaper at day N
  dearer 5%+      share of cards at least 5% dearer at day N
  best wait       median saving from buying at the lowest price in the window
                  (hindsight; an upper bound on what waiting could save)
  vs all          label median minus the all-cards median on the same dates,
                  with a 90% interval from resampling whole sets (cards in a
                  set move together, so they are not independent)

Dates inside the window the score was tuned on are left out by default, so
the numbers are out of sample. Use --all to include them.

  python tracker/backtest.py            # 30-day look-ahead, out-of-sample dates
  python tracker/backtest.py 60 --all

No network needed. Writes report/backtest.json and prints a summary.
"""
import datetime as dt
import glob
import json
import math
import random
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import track  # noqa: E402
from score import score_card, supply_signal, median  # noqa: E402

ROOT = track.ROOT
TUNED = ("2026-09-08", "2026-11-12")   # score settings were chosen looking at outcomes in this window
NAN = float("nan")


def load():
    cfg = json.loads((ROOT / "config.json").read_text())
    report = json.loads((ROOT / "report" / "latest.json").read_text())
    pub = {s["name"]: s["published"] for s in report["sets"]}
    cards, set_of, sealed_of, set_id = {}, {}, {}, {}
    for f in glob.glob(str(track.PROD_DIR / "*.json")):
        d = json.loads(Path(f).read_text())
        if d["set"] not in pub:
            continue
        set_id[d["set"]] = int(Path(f).stem)
        for pid, c in d["cards"].items():
            cards[int(pid)] = c
            set_of[int(pid)] = d["set"]
        for pid, name in d.get("sealed", {}).items():
            sealed_of[int(pid)] = (d["set"], name)
    days = track.snapshot_days()
    # one float array per (product, printing): price on each day, NaN when missing
    prices = {}
    for i, d in enumerate(days):
        for k, (m, _) in track.read_snapshot(track.SNAP_DIR / f"{d}.csv.gz").items():
            if k[0] in set_of or k[0] in sealed_of:
                a = prices.get(k)
                if a is None:
                    a = prices[k] = array("f", [NAN]) * len(days)
                a[i] = m
    return cfg, pub, cards, set_of, sealed_of, set_id, days, prices


def series_at(a, i):
    return [(i - j, a[j]) for j in range(max(0, i - 93), i) if not math.isnan(a[j])]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ahead = int(args[0]) if args else 30
    use_all = "--all" in sys.argv
    cfg, pub, cards, set_of, sealed_of, set_id, days, prices = load()
    undated = set(cfg.get("undated_set_ids", []))
    date = [dt.date.fromisoformat(d) for d in days]
    idx = {d: i for i, d in enumerate(days)}

    # weekly test dates, newest first, needing 90 days behind and `ahead` days in front
    tests = []
    i = len(days) - 1 - ahead
    while i >= 0 and (date[i] - date[0]).days >= 60:
        D = days[i]
        end = (date[i] + dt.timedelta(days=ahead)).isoformat()
        overlaps = not (end < TUNED[0] or D > TUNED[1])
        if (date[i] + dt.timedelta(days=ahead)).isoformat() in idx and (use_all or not overlaps):
            tests.append(i)
        i -= 7
    if not tests:
        sys.exit("not enough out-of-sample history yet (add --all to include the tuning window)")

    first_seen = {k: next(n for n in range(len(days)) if not math.isnan(a[n])) for k, a in prices.items()}
    rows = []   # (label, set, fwd change, best saving, date)
    for i in tests:
        D = date[i]
        j = idx[(D + dt.timedelta(days=ahead)).isoformat()]
        by_set = {}
        for k, a in prices.items():
            if k[0] in sealed_of and not math.isnan(a[i]):
                s, name = sealed_of[k[0]]
                by_set.setdefault(s, []).append((name, a[i], series_at(a, i)))
        supply = {s: sig for s, items in by_set.items() if (sig := supply_signal(items))}
        for k, a in prices.items():
            if k[0] not in set_of:
                continue
            m, f = a[i], a[j]
            if math.isnan(m) or math.isnan(f) or m < cfg["min_price"]:
                continue
            name = set_of[k[0]]
            if pub[name] > days[i]:
                continue
            first = first_seen[k]
            age = None if first == 0 else (i - first)
            months = (D - dt.date.fromisoformat(pub[name])).days / 30.44
            sc = score_card(m, series_at(a, i), months, set_id[name] in undated, age, supply.get(name))
            if not sc:
                continue
            window = [a[n] for n in range(i + 1, j + 1) if not math.isnan(a[n])]
            best = (m - min(window)) / m if window else 0.0
            rows.append((sc["label"], name, f / m - 1, best, days[i]))

    def stats(sub):
        ch = [r[2] for r in sub]
        return {"cards": len(ch), "median_change_pct": round(median(ch) * 100, 1),
                "cheaper5_pct": round(sum(c <= -0.05 for c in ch) / len(ch) * 100),
                "dearer5_pct": round(sum(c >= 0.05 for c in ch) / len(ch) * 100),
                "best_wait_saving_pct": round(median([r[3] for r in sub]) * 100, 1)}

    sets = sorted({r[1] for r in rows})
    by_set = {s: [r for r in rows if r[1] == s] for s in sets}
    rng = random.Random(7)

    def gap_interval(label, reps=300):
        """90% interval for (label median - all-cards median), resampling whole sets."""
        diffs = []
        for _ in range(reps):
            pick = [rng.choice(sets) for _ in sets]
            allc = [r[2] for s in pick for r in by_set[s]]
            lab = [r[2] for s in pick for r in by_set[s] if r[0] == label]
            if len(lab) >= 20:
                diffs.append((median(lab) - median(allc)) * 100)
        diffs.sort()
        if len(diffs) < 50:
            return None
        return [round(diffs[int(0.05 * len(diffs))], 1), round(diffs[int(0.95 * len(diffs)) - 1], 1)]

    out = {"look_ahead_days": ahead, "out_of_sample_only": not use_all, "tuned_window": TUNED,
           "dates_tested": [days[i] for i in tests], "history": [days[0], days[-1]],
           "all": stats(rows), "by_label": {}}
    base = out["all"]["median_change_pct"]
    for label in sorted({r[0] for r in rows}):
        sub = [r for r in rows if r[0] == label]
        s = stats(sub)
        s["vs_all_pct"] = round(s["median_change_pct"] - base, 1)
        s["vs_all_90pct_interval"] = gap_interval(label)
        out["by_label"][label] = s
    out["by_date"] = {}
    for i in tests:
        sub = [r for r in rows if r[4] == days[i]]
        st = [r for r in sub if r[0] == "Stable price"]
        out["by_date"][days[i]] = {"all": round(median([r[2] for r in sub]) * 100, 1),
                                   "stable": round(median([r[2] for r in st]) * 100, 1) if st else None}
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "backtest.json").write_text(json.dumps(out, indent=1))

    print(f"look-ahead {ahead} days, {len(tests)} test dates {days[tests[-1]]} .. {days[tests[0]]}"
          f"{'' if use_all else ' (out of sample)'}, {len(rows)} card-dates, {len(sets)} sets")
    fmt = "{:22} {:>7} {:>8} {:>9} {:>8} {:>10} {:>8} {:>14}"
    print(fmt.format("", "cards", "median%", "cheaper%", "dearer%", "bestwait%", "vs all", "90% interval"))
    print(fmt.format("all cards", *[out["all"][k] for k in ("cards", "median_change_pct", "cheaper5_pct",
                                                            "dearer5_pct", "best_wait_saving_pct")], "", ""))
    for name, s in out["by_label"].items():
        print(fmt.format(name, s["cards"], s["median_change_pct"], s["cheaper5_pct"], s["dearer5_pct"],
                         s["best_wait_saving_pct"], s["vs_all_pct"], str(s["vs_all_90pct_interval"])))
    print("by date (median change, all vs stable):",
          ", ".join(f"{d}: {v['all']}/{v['stable']}" for d, v in out["by_date"].items()))


if __name__ == "__main__":
    main()
