"""Price check: is today's price low for this card, judged by its own history?

For each card the window is its market prices over the last 365 days
(today included). From that window:

  low / high / typical (median) price
  good price (target): the 20th percentile of the window, i.e. the card was
      at or below this price on only a fifth of its days in the past year
  at good price: today's price is at or below the target, and in the cheapest
      fifth of the window counting ties as half (so a price that never moves
      does not count)

This describes where today's price sits in the card's own history. It is not
a prediction. The scorecard below measures, on past dates, how often a card
that was at its good price then went another 10% or more lower within 60
days, so you can see how much confidence the target deserves.
"""
import bisect
import json
import math

WINDOW_DAYS = 365
TARGET_PCTILE = 0.20
MIN_DAYS = 90            # days of the card's own prices in the window before a target is set
SPARK_STEP = 14          # one chart point every two weeks
OUTLOOK_DAYS = 60        # scorecard looks this far ahead
DEEPER = 0.10            # "went another 10%+ lower"
RULE = f"p{int(TARGET_PCTILE * 100)} of {WINDOW_DAYS}d, min {MIN_DAYS}d, outlook {OUTLOOK_DAYS}d, deeper {int(DEEPER * 100)}%"


def window(arr, ords, i):
    """Prices in the 365 days up to and including index i."""
    lo = bisect.bisect_left(ords, ords[i] - WINDOW_DAYS + 1)
    return [arr[j] for j in range(lo, i + 1) if not math.isnan(arr[j])]


def check(arr, ords, i):
    """Price check for the card at day index i, or None if too little history."""
    if math.isnan(arr[i]):
        return None
    w = window(arr, ords, i)
    if len(w) < MIN_DAYS:
        return None
    s = sorted(w)
    target = s[max(0, math.ceil(TARGET_PCTILE * len(s)) - 1)]
    price = arr[i]
    # mid-rank: a price that never moves sits in the middle of its range, not at the bottom,
    # so thinly traded cards with a stale price are not reported as "at a good price"
    below = sum(1 for x in w if x < price - 1e-9)
    equal = sum(1 for x in w if abs(x - price) <= 1e-9)
    rank = (below + 0.5 * equal) / len(w)
    return {"price": price, "low": s[0], "high": s[-1], "med": s[len(s) // 2], "target": target,
            "days": len(w), "at": price <= target + 1e-9 and rank <= TARGET_PCTILE,
            "flat": s[-1] - s[0] < 0.01 * s[-1]}


def spark(arr, ords, i):
    """About 27 points: the last price in each two-week step over the window, oldest first."""
    pts, j = [], i
    end = ords[i]
    while end > ords[i] - WINDOW_DAYS:
        lo = bisect.bisect_left(ords, end - SPARK_STEP + 1)
        val = None
        for k in range(min(j, len(arr) - 1), lo - 1, -1):
            if ords[k] <= end and not math.isnan(arr[k]):
                val = arr[k]
                break
        pts.append(None if val is None else round(val, 2))
        end -= SPARK_STEP
    pts.reverse()
    while pts and pts[0] is None:
        pts.pop(0)
    return pts


def _price_near(arr, ords, i, back, slack=7):
    """Price about `back` days before index i (closest within back..back+slack)."""
    target = ords[i] - back
    j = bisect.bisect_right(ords, target) - 1
    while j >= 0 and ords[j] >= target - slack:
        if not math.isnan(arr[j]):
            return arr[j]
        j -= 1
    return None


def scorecard(matrix, ords, days, min_price, cache_path, rule_since):
    """How cards at their good price did over the next 60 days, on weekly past
    dates. Finished dates are cached in cache_path and never recomputed (unless
    the rule changes). Dates on or after rule_since count as 'live': the rule
    was fixed before those outcomes existed."""
    cache = {}
    if cache_path.exists():
        c = json.loads(cache_path.read_text())
        if c.get("rule") == RULE:
            cache = c.get("dates", {})
    last = ords[-1]
    for i in range(len(days)):
        d = days[i]
        if d in cache or (ords[i] - ords[0]) % 7 or ords[i] + OUTLOOK_DAYS > last:
            continue
        if ords[i] - ords[0] < MIN_DAYS:
            continue
        end = bisect.bisect_right(ords, ords[i] + OUTLOOK_DAYS)
        st = {"at": 0, "at_deeper": 0, "all": 0, "all_deeper": 0,
              "at_falling": 0, "at_falling_deeper": 0, "at_steady": 0, "at_steady_deeper": 0,
              "at_buckets": [0, 0, 0, 0]}   # further drop: <5%, 5-10%, 10-20%, 20%+
        for arr in matrix.values():
            p = arr[i]
            if math.isnan(p) or p < min_price:
                continue
            fut = [arr[j] for j in range(i + 1, end) if not math.isnan(arr[j])]
            if len(fut) < 20:
                continue
            c = check(arr, ords, i)
            if not c:
                continue
            drop = 1 - min(fut) / p
            deeper = drop >= DEEPER
            st["all"] += 1
            st["all_deeper"] += deeper
            if c["at"]:
                st["at"] += 1
                st["at_deeper"] += deeper
                st["at_buckets"][0 if drop < 0.05 else 1 if drop < 0.10 else 2 if drop < 0.20 else 3] += 1
                p30 = _price_near(arr, ords, i, 28)
                falling = p30 is not None and p / p30 - 1 <= -0.10
                key = "at_falling" if falling else "at_steady"
                st[key] += 1
                st[key + "_deeper"] += deeper
        cache[d] = st
    cache_path.write_text(json.dumps({"rule": RULE, "dates": dict(sorted(cache.items()))}, separators=(",", ":")))

    def total(dates):
        t = {"dates": len(dates)}
        for k in ("at", "at_deeper", "all", "all_deeper", "at_falling", "at_falling_deeper", "at_steady", "at_steady_deeper"):
            t[k] = sum(cache[d][k] for d in dates)
        t["at_buckets"] = [sum(cache[d]["at_buckets"][b] for d in dates) for b in range(4)]
        if dates:
            t["from"], t["to"] = min(dates), max(dates)
        return t

    dates = sorted(cache)
    return {"rule": RULE, "outlook_days": OUTLOOK_DAYS, "deeper_pct": int(DEEPER * 100),
            "all_dates": total(dates), "live": total([d for d in dates if d >= rule_since]), "rule_since": rule_since}
