"""Re-test the live Buy/Wait rule against nearby settings on the price history.

    python3 tools/backtest.py            # writes backtests/<today>.md and prints a short verdict

How it judges: every setting is run through the same evidence() the dashboard
uses (weekly past dates, 60-day outlook, "saved" = what following the labels
saved versus buying every card that day, dollar-weighted). The most recent 12
months of checks are held back as the test; everything before is training.
A setting is a candidate only if it beats the live rule on BOTH training and
test by at least MARGIN points, is no worse in its worst half-year, and also
beats the live rule at the 30- and 90-day horizons. Nothing is changed in the
rule: the report says what to change and why.
"""
import collections
import datetime as dt
import glob
import json
import math
import sys
import time
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tracker"))
import buywait as bw  # noqa: E402
import track  # noqa: E402

MARGIN = 0.2          # percentage points of "saved"
TUNABLE = ["DROP_7", "DROP_28", "JUMP_7", "JUMP_28", "SET_FALL", "SET_RECOVER", "SETTLE_DAYS", "NEW_SET_DAYS", "NEW_CARD_DAYS", "OUTLOOK"]


def load():
    R = json.loads((ROOT / "report" / "latest.json").read_text())
    cfg = json.loads((ROOT / "config.json").read_text())
    undated = set(cfg.get("undated_set_ids", []))
    meta = {s["name"]: s for s in R["sets"]}
    cards, info = {}, {}
    for f in glob.glob(str(ROOT / "data" / "products" / "*.json")):
        d = json.loads(Path(f).read_text())
        if d["set"] not in meta:
            continue
        info[d["set"]] = {"published": meta[d["set"]]["published"], "undated": int(Path(f).stem) in undated}
        for p in d["cards"]:
            cards[p] = d["set"]
    days = track.snapshot_days()
    cur = track.read_snapshot(track.SNAP_DIR / f"{days[-1]}.csv.gz")
    keep = {k for k, (m, _) in cur.items() if str(k[0]) in cards and m >= bw.MIN_PRICE}
    M = track.load_matrix(days, keep)
    return days, M, {k: cards[str(k[0])] for k in M}, info


def variants(live, live_market):
    v = {"live rule": {}}
    d7, d28 = live["DROP_7"], live["DROP_28"]
    v["still dropping 1 pt tighter"] = {"DROP_7": d7 + 0.01, "DROP_28": d28 + 0.01}
    v["still dropping 1 pt looser"] = {"DROP_7": d7 - 0.01, "DROP_28": d28 - 0.01}
    v["still dropping 2 pts looser"] = {"DROP_7": d7 - 0.02, "DROP_28": d28 - 0.02}
    if live["JUMP_7"] is None:
        v["'just jumped' back on (8%/20%)"] = {"JUMP_7": 0.08, "JUMP_28": 0.20}
    else:
        v["'just jumped' off"] = {"JUMP_7": None, "JUMP_28": None}
    v["set falling at -2%"] = {"SET_FALL": live["SET_FALL"] + 0.01}
    v["set falling at -4%"] = {"SET_FALL": live["SET_FALL"] - 0.01}
    v["settle 3 days"] = {"SETTLE_DAYS": 3}
    v["settle 14 days"] = {"SETTLE_DAYS": 14}
    v["new set 120 days"] = {"NEW_SET_DAYS": 120}
    v["new set 270 days"] = {"NEW_SET_DAYS": 270}
    if live_market:
        v["market switch at +1%"] = {"_market": dict(live_market, rise=live_market["rise"] - 0.01)}
        v["market switch at +4%"] = {"_market": dict(live_market, rise=live_market["rise"] + 0.02)}
        v["market switch off"] = {"_market": None}
    return v


def run(days, M, card_set, info, live, live_market, over, horizon=None):
    p = dict(live)
    p.update({k: x for k, x in over.items() if not k.startswith("_")})
    if horizon:
        p["OUTLOOK"] = horizon
    for k, x in p.items():
        setattr(bw, k, x)
    market = over.get("_market", live_market) if "_market" in over else live_market
    m = {k: array("f", a) for k, a in M.items()}
    e = bw.Engine(m, days, card_set, info, market_rule=market)
    out = Path("/tmp/backtest_ev.json")
    out.unlink(missing_ok=True)
    e.evidence(out, "9999-12-31")
    for k, x in live.items():
        setattr(bw, k, x)
    return json.loads(out.read_text())["dates"]


def agg(c, ds):
    if not ds:
        return None
    now = sum(c[d]["now"] for d in ds)
    paid = sum(c[d]["paid"] for d in ds)
    nb = sum(c[d]["buy"][0] for d in ds)
    nw = sum(c[d]["wait"][0] for d in ds)
    return {"saved": round((now - paid) / now * 100, 2) if now else None, "buy_share": round(nb * 100 / (nb + nw)) if nb + nw else None}


def score(c, split):
    ds = sorted(c)
    halves = collections.defaultdict(list)
    for d in ds:
        halves[d[:4] + (" H1" if d[5:7] < "07" else " H2")].append(d)
    hv = {h: agg(c, x)["saved"] for h, x in sorted(halves.items()) if len(x) >= 4}
    return {"train": agg(c, [d for d in ds if d < split]), "test": agg(c, [d for d in ds if d >= split]),
            "worst_half": min(hv.values()) if hv else None, "halves": hv}


def main():
    t0 = time.time()
    live = {k: getattr(bw, k) for k in TUNABLE}
    live_market = bw.MARKET_RULE if json.loads((ROOT / "config.json").read_text()).get("market_aware", True) else None
    days, M, card_set, info = load()
    last = dt.date.fromisoformat(days[-1])
    split = (last - dt.timedelta(days=365 + live["OUTLOOK"])).isoformat()   # last 12 months of checkable dates = test
    V = variants(live, live_market)
    res = {}
    for name, over in V.items():
        c = run(days, M, card_set, info, live, live_market, over)
        res[name] = score(c, split)
        print(f"{name:34} train {res[name]['train']['saved']:+.2f} test {res[name]['test']['saved']:+.2f} "
              f"worst half {res[name]['worst_half']:+.2f} buy share {res[name]['test']['buy_share']}%", flush=True)
    base = res["live rule"]
    cands = [n for n, r in res.items() if n != "live rule"
             and r["train"]["saved"] >= base["train"]["saved"] + MARGIN
             and r["test"]["saved"] >= base["test"]["saved"] + MARGIN
             and r["worst_half"] >= base["worst_half"]]
    robust = {}
    for n in cands:       # must also win at 30 and 90 days
        ok = True
        for h in (30, 90):
            b = score(run(days, M, card_set, info, live, live_market, {}, h), split)
            x = score(run(days, M, card_set, info, live, live_market, V[n], h), split)
            ok &= x["train"]["saved"] > b["train"]["saved"] and x["test"]["saved"] > b["test"]["saved"]
        robust[n] = ok
    winners = [n for n in cands if robust[n]]
    winners.sort(key=lambda n: -(res[n]["train"]["saved"] + res[n]["test"]["saved"]))

    today = dt.date.today().isoformat()
    lines = [f"# Buy/Wait rule re-test ({today})", "",
             f"Live rule: {bw.RULE}", f"Price history {days[0]} to {days[-1]}; test = checks from {split} on (last 12 months with a full {live['OUTLOOK']}-day outlook), train = before.",
             f"Candidate = beats the live rule by {MARGIN}+ points on both train and test, no worse worst half-year, and also wins at 30 and 90 days.", "",
             "| Setting | Train saved | Test saved | Worst half-year | Buy-first share (test) |", "|---|---|---|---|---|"]
    for n, r in res.items():
        mark = " **(candidate)**" if n in winners else ""
        lines.append(f"| {n}{mark} | {r['train']['saved']:.2f}% | {r['test']['saved']:.2f}% | {r['worst_half']:.2f}% | {r['test']['buy_share']}% |")
    lines += ["", "Half-years for the live rule: " + ", ".join(f"{h} {v:+.2f}%" for h, v in base["halves"].items()), ""]
    verdict = (f"Change suggested: {winners[0]} (train {res[winners[0]]['train']['saved']:.2f}% vs {base['train']['saved']:.2f}%, "
               f"test {res[winners[0]]['test']['saved']:.2f}% vs {base['test']['saved']:.2f}%). Not applied; ask Claude to adopt it."
               if winners else "No change: no nearby setting clearly beats the live rule on data it wasn't tuned on.")
    lines += ["**Verdict:** " + verdict, ""]
    out = ROOT / "backtests" / f"{today}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"\n{verdict}\nwrote {out.relative_to(ROOT)} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
