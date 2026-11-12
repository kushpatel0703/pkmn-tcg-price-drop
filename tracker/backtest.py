#!/usr/bin/env python3
"""Backtest the buy score on the price history already in data/snapshots/.

For a series of past dates it scores every card using only what was known on
that date, then looks at what the market price did over the following days.
For a collector the question is: was buying on that date better than waiting?

  python tracker/backtest.py            # 30-day look-ahead
  python tracker/backtest.py 60         # 60-day look-ahead

No network needed. Writes report/backtest.json and prints a summary.
"""
import datetime as dt
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import track  # noqa: E402
from score import score_card, supply_signal, median  # noqa: E402

ROOT = track.ROOT


def load_sets(cfg):
    report = json.loads((ROOT / "report" / "latest.json").read_text())
    pub = {s["name"]: s["published"] for s in report["sets"]}
    cards, set_of, sealed, set_id = {}, {}, {}, {}
    for f in glob.glob(str(track.PROD_DIR / "*.json")):
        d = json.loads(Path(f).read_text())
        if d["set"] not in pub:
            continue
        set_id[d["set"]] = int(Path(f).stem)
        sealed[d["set"]] = d.get("sealed", {})
        for pid in d["cards"]:
            cards[pid] = d["cards"][pid]
            set_of[pid] = d["set"]
    return pub, cards, set_of, sealed, set_id


def main():
    ahead = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    cfg = json.loads((ROOT / "config.json").read_text())
    pub, cards, set_of, sealed, set_id = load_sets(cfg)
    undated = set(cfg.get("undated_set_ids", []))
    days = track.snapshot_days()
    date = {d: dt.date.fromisoformat(d) for d in days}
    snaps = {}
    for d in days:  # keep only tracked cards and sealed product to save memory
        s = track.read_snapshot(track.SNAP_DIR / f"{d}.csv.gz")
        snaps[d] = {k: v for k, v in s.items()
                    if str(k[0]) in cards or any(str(k[0]) in sl for sl in sealed.values())}
    sealed_set = {pid: name for name, sl in sealed.items() for pid in sl}

    first, last = date[days[0]], date[days[-1]]
    eval_days = []
    d = last - dt.timedelta(days=ahead)
    while d >= first + dt.timedelta(days=25):
        if d.isoformat() in snaps and (d + dt.timedelta(days=ahead)).isoformat() in snaps:
            eval_days.append(d)
        d -= dt.timedelta(days=7)
    if not eval_days:
        sys.exit("not enough history for this look-ahead yet")

    rows = []  # (label, score, forward change, date)
    for D in eval_days:
        cur = snaps[D.isoformat()]
        fut = snaps[(D + dt.timedelta(days=ahead)).isoformat()]
        series = {}
        for d2 in days:
            age = (D - date[d2]).days
            if 1 <= age <= 93:
                for k, (m, _) in snaps[d2].items():
                    if k in cur:
                        series.setdefault(k, []).append((age, m))
        by_set = {}
        for k, (m, _) in cur.items():
            pid = str(k[0])
            if pid in sealed_set:
                by_set.setdefault(sealed_set[pid], []).append((sealed[sealed_set[pid]][pid], m, series.get(k, [])))
        supply = {name: sig for name, items in by_set.items() if (sig := supply_signal(items))}
        for k, (m, low) in cur.items():
            pid = str(k[0])
            if pid not in cards or m < cfg["min_price"] or k not in fut:
                continue
            name = set_of[pid]
            months = (D - dt.date.fromisoformat(pub[name])).days / 30.44
            sc = score_card(m, low, series.get(k, []), months, set_id[name] in undated,
                            cfg.get("buyable_within_pct", 3), supply.get(name))
            if sc:
                rows.append((sc["label"], sc["score"], fut[k][0] / m - 1, D.isoformat()))

    def stats(sub):
        ch = [r[2] for r in sub]
        return {"cards": len(ch),
                "median_change_pct": round(median(ch) * 100, 1),
                "mean_change_pct": round(sum(ch) / len(ch) * 100, 1),
                "cheaper_later_pct": round(sum(c <= -0.05 for c in ch) / len(ch) * 100),   # waiting saved 5%+
                "dearer_later_pct": round(sum(c >= 0.05 for c in ch) / len(ch) * 100)}     # waiting cost 5%+

    out = {"look_ahead_days": ahead, "dates_tested": [d.isoformat() for d in eval_days],
           "history": [days[0], days[-1]], "all": stats(rows), "by_label": {}, "by_score": {}}
    for label in sorted({r[0] for r in rows}):
        out["by_label"][label] = stats([r for r in rows if r[0] == label])
    for lo, hi in ((0, 60), (60, 70), (70, 80), (80, 90), (90, 101)):
        sub = [r for r in rows if lo <= r[1] < hi]
        if sub:
            out["by_score"][f"{lo}-{min(hi, 100)}"] = stats(sub)
    (ROOT / "report").mkdir(exist_ok=True)
    (ROOT / "report" / "backtest.json").write_text(json.dumps(out, indent=1))

    print(f"look-ahead {ahead} days, {len(eval_days)} test dates {eval_days[-1]} .. {eval_days[0]}, {len(rows)} card-dates")
    fmt = "{:30} {:>7} {:>8} {:>8} {:>9} {:>8}"
    print(fmt.format("", "cards", "median%", "mean%", "cheaper%", "dearer%"))
    for title, table in (("ALL", {"all": out["all"]}), ("BY VERDICT", out["by_label"]), ("BY SCORE", out["by_score"])):
        print(title)
        for name, s in table.items():
            print(fmt.format(name, s["cards"], s["median_change_pct"], s["mean_change_pct"],
                             s["cheaper_later_pct"], s["dearer_later_pct"]))


if __name__ == "__main__":
    main()
