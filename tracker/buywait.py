"""Buy / Wait: should a collector buy this card now, or is waiting likely to pay?

Each card worth $1 or more gets one state per day, from data up to that day only.

  Wait, with one reason (first that applies):
    New set        the set came out less than 6 months ago (dated sets only)
    Just listed    the card was first priced less than 45 days ago
    Set falling    the median card in the set is down 3%+ over 8 weeks; the set
                   stays "falling" until that recovers above -1%
    Still dropping the card is down 8%+ in a week or 10%+ in 4 weeks
    Just jumped    the card is up 8%+ in a week or 20%+ in 4 weeks
  Buy: none of those applied on any of the last 7 days.

  While the whole market is rising (median card up 2%+ over 8 weeks), only
  "Still dropping" holds a card back: in rising markets waiting cost money.
  Chosen on 2024-2025 checks and confirmed on 2026 (saved 1.6% vs 0.8% of spend
  against buying everything at once).

Why set-led: in the backtests (Jun-Nov 2026) a card's position in its own price
range did not predict whether waiting would pay (cards at their lows kept
falling), while a falling or new set did: its cards were a median ~9% cheaper
60 days later. "Buy" means waiting has saved little on cards like this; it is
not a promise the price will not dip. The evidence is recomputed every day from
the history on file (see evidence()).

Prices from before a set's release date are ignored.
"""
import bisect
import datetime as dt
import json
import math

MIN_PRICE = 1.0
NEW_SET_DAYS = 182
NEW_CARD_DAYS = 45
SET_FALL, SET_RECOVER = -0.03, -0.01
SET_MIN_CARDS = 5
DROP_7, DROP_28 = -0.08, -0.10
JUMP_7, JUMP_28 = 0.08, 0.20
SETTLE_DAYS = 7
OUTLOOK = 60
RULE = ("v2: new<182d, listed<45d, set8w<=-3% (recover -1%), drop 7d-8%/28d-10%, jump 7d+8%/28d+20%, settle 7d; "
        "market8w>=+2%: only dropping")

NONE, NEW_SET, NEW_CARD, SET_FALLING, DROPPING, JUMPED = 0, 1, 2, 3, 4, 5
MARKET_RULE = {"rise": 0.02, "skip": {NEW_SET, NEW_CARD, SET_FALLING, JUMPED}}
NAN = float("nan")
SEALED_KEEP = ("booster box",)
SEALED_SKIP = ("case", "display", "half", "pokemon center", "code card", "lot of", "bundle", "mini")


def _back_index(ords, back, slack):
    """For each day i, the latest index whose date is between back+slack and back days earlier."""
    out = []
    for o in ords:
        j = bisect.bisect_right(ords, o - back) - 1
        out.append(j if j >= 0 and ords[j] >= o - back - slack else -1)
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2)


class Engine:
    """Computes daily states for every card from the price matrix."""

    def __init__(self, matrix, days, card_set, set_info, market_rule=None):
        """matrix: {key: array of market prices per day}; card_set: {key: set name};
        set_info: {set name: {"published": iso date, "undated": bool}}."""
        self.days, self.n = days, len(days)
        self.ords = [dt.date.fromisoformat(d).toordinal() for d in days]
        self.m, self.card_set, self.set_info = matrix, card_set, set_info
        # market_rule: None, or {"rise": 0.02, "skip": {SET_FALLING, ...}}: while the whole market's
        # median 8-week change is at or above "rise", the listed Wait reasons are not applied
        self.market_rule = market_rule
        self.j7 = _back_index(self.ords, 7, 3)
        self.j28 = _back_index(self.ords, 28, 7)
        self.j56 = _back_index(self.ords, 56, 7)
        self.rel = {s: dt.date.fromisoformat(v["published"]).toordinal() for s, v in set_info.items()}
        # ignore prices from before a dated set's release
        for k, a in matrix.items():
            info = set_info.get(card_set[k])
            if info and not info["undated"]:
                r = self.rel[card_set[k]]
                for i in range(self.n):
                    if self.ords[i] >= r:
                        break
                    a[i] = NAN
        self.first = {}
        for k, a in matrix.items():
            self.first[k] = next((i for i in range(self.n) if not math.isnan(a[i])), None)
        self._set_trend()
        self._market_trend()
        self._states()

    def _set_trend(self):
        members = {}
        for k, s in self.card_set.items():
            members.setdefault(s, []).append(self.m[k])
        self.chg8, self.falling = {}, {}
        for s, arrs in members.items():
            chg, fall, prev = [None] * self.n, bytearray(self.n), 0
            for i in range(self.n):
                j = self.j56[i]
                if j >= 0:
                    r = [a[i] / a[j] - 1 for a in arrs
                         if not math.isnan(a[i]) and a[i] >= MIN_PRICE and not math.isnan(a[j]) and a[j] > 0]
                    if len(r) >= SET_MIN_CARDS:
                        chg[i] = _median(r)
                c = chg[i]
                if c is None:
                    prev = 0
                else:
                    prev = 1 if (c <= SET_FALL or (prev and c <= SET_RECOVER)) else 0
                fall[i] = prev
            self.chg8[s], self.falling[s] = chg, fall

    def _market_trend(self):
        """Median 8-week change across all cards $1+ on each day."""
        arrs = list(self.m.values())
        self.mkt8 = [None] * self.n
        for i in range(self.n):
            j = self.j56[i]
            if j < 0:
                continue
            r = [a[i] / a[j] - 1 for a in arrs
                 if not math.isnan(a[i]) and a[i] >= MIN_PRICE and not math.isnan(a[j]) and a[j] > 0]
            if len(r) >= 50:
                self.mkt8[i] = _median(r)

    def rising(self, i):
        mr = self.market_rule
        return bool(mr) and self.mkt8[i] is not None and self.mkt8[i] >= mr["rise"]

    def reason_at(self, k, i):
        a, s = self.m[k], self.card_set[k]
        p = a[i]
        if math.isnan(p) or p < MIN_PRICE:
            return None
        skip = self.market_rule["skip"] if self.rising(i) else ()
        info = self.set_info.get(s, {})
        if NEW_SET not in skip and not info.get("undated") and self.ords[i] - self.rel[s] < NEW_SET_DAYS:
            return NEW_SET
        f = self.first[k]
        if NEW_CARD not in skip and f is not None and f > 0 and self.ords[i] - self.ords[f] < NEW_CARD_DAYS:
            return NEW_CARD
        if SET_FALLING not in skip and self.falling[s][i]:
            return SET_FALLING
        c7 = self.change(k, i, self.j7)
        c28 = self.change(k, i, self.j28)
        if DROPPING not in skip and ((c7 is not None and c7 <= DROP_7) or (c28 is not None and c28 <= DROP_28)):
            return DROPPING
        if JUMPED not in skip and ((c7 is not None and c7 >= JUMP_7) or (c28 is not None and c28 >= JUMP_28)):
            return JUMPED
        return NONE

    def change(self, k, i, jarr):
        j = jarr[i]
        if j < 0:
            return None
        a = self.m[k]
        if math.isnan(a[i]) or math.isnan(a[j]) or a[j] <= 0:
            return None
        return a[i] / a[j] - 1

    def _states(self):
        """state[k][i]: 0 no state (no price / under $1), 1 Buy, 2 Wait; reason[k][i]: reason code."""
        self.state, self.reason = {}, {}
        for k in self.m:
            st, rs = bytearray(self.n), bytearray(self.n)
            last_wait = None
            for i in range(self.n):
                r = self.reason_at(k, i)
                if r is None:
                    continue
                rs[i] = r
                if r != NONE:
                    last_wait = self.ords[i]
                st[i] = 2 if (last_wait is not None and self.ords[i] - last_wait < SETTLE_DAYS) else 1
            self.state[k], self.reason[k] = st, rs

    # ---- describing today
    def describe(self, k, i):
        """State, short reason text and days in the current state, for day i."""
        st, rs = self.state[k][i], self.reason[k][i]
        if st == 0:
            return None
        s = self.card_set[k]
        days_in = 0
        for j in range(i, -1, -1):
            if self.state[k][j] != st and self.state[k][j] != 0:
                break
            days_in = self.ords[i] - self.ords[j]
        if st == 1:
            wk = days_in // 7
            return {"state": "Buy", "reason": f"steady {wk} wk{'s' if wk != 1 else ''}" if wk else "steady", "days": days_in}
        if rs == NEW_SET:
            text = "New set (" + dt.date.fromordinal(self.rel[s]).strftime("%b %Y") + ")"
        elif rs == NEW_CARD:
            text = "Just listed"
        elif rs == SET_FALLING:
            c = self.chg8[s][i]
            text = f"Set falling {round(c * 100):+d}% / 8 wks" if c is not None else "Set falling"
        elif rs == DROPPING:
            c28, c7 = self.change(k, i, self.j28), self.change(k, i, self.j7)
            text = f"Still dropping {round(c28 * 100):+d}% / 4 wks" if c28 is not None and c28 <= DROP_28 \
                else f"Still dropping {round(c7 * 100):+d}% / 1 wk"
        elif rs == JUMPED:
            c28, c7 = self.change(k, i, self.j28), self.change(k, i, self.j7)
            text = f"Just jumped {round(c7 * 100):+d}% / 1 wk" if c7 is not None and c7 >= JUMP_7 \
                else f"Just jumped {round(c28 * 100):+d}% / 4 wks"
        else:
            last = next((j for j in range(i, -1, -1) if self.reason[k][j] not in (NONE,) and self.state[k][j]), i)
            text = f"Settling: last warning {self.ords[i] - self.ords[last]} days ago"
        return {"state": "Wait", "reason": text, "days": days_in}

    def set_state(self, s, i):
        info = self.set_info.get(s, {})
        new = not info.get("undated") and self.ords[i] - self.rel[s] < NEW_SET_DAYS
        c = self.chg8.get(s, [None] * self.n)[i]
        return {"new": bool(new), "falling": bool(self.falling.get(s, bytearray(self.n))[i]),
                "chg8w": None if c is None else round(c * 100, 1)}

    # ---- evidence from the history on file
    def evidence(self, cache_path, rule_since):
        """For weekly past dates (Mondays) with 60 days of outcome: how cards labelled
        Buy and Wait moved, and what following the labels paid versus buying at
        once, both against the lowest price in the 60 days. Finished dates are
        cached; the cache resets when the rule or the start of the history changes."""
        key = {"rule": RULE, "history_from": self.days[0]}
        cache = {}
        try:
            c = json.loads(cache_path.read_text())
            if c.get("key") == key:
                cache = c["dates"]
        except (OSError, ValueError, KeyError):
            pass
        last = self.ords[-1]
        for i in range(self.n):
            o = self.ords[i]
            d = self.days[i]
            if d in cache or o % 7 != 0 or o + OUTLOOK > last or o - self.ords[0] < 56:
                continue
            e = bisect.bisect_right(self.ords, o + OUTLOOK) - 1
            agg = {"buy": [0, 0.0, 0.0, 0, 0], "wait": [0, 0.0, 0.0, 0, 0], "paid": 0.0, "now": 0.0, "low": 0.0}
            for k, a in self.m.items():
                st = self.state[k][i]
                if st == 0:
                    continue
                p0 = a[i]
                fut = [(j, a[j]) for j in range(i, e + 1) if not math.isnan(a[j])]
                if len(fut) < 20:
                    continue
                p60 = fut[-1][1]
                low = min(v for _, v in fut)
                b = agg["buy" if st == 1 else "wait"]
                b[0] += 1
                b[1] += p0
                b[2] += p60
                b[3] += p60 <= 0.95 * p0
                b[4] += p60 >= 1.05 * p0
                paid = next((v for j, v in fut if self.state[k][j] == 1), p60)
                agg["paid"] += paid
                agg["now"] += p0
                agg["low"] += low
            cache[d] = agg
        cache_path.write_text(json.dumps({"key": key, "dates": dict(sorted(cache.items()))}, separators=(",", ":")))

        def summary(dates):
            if not dates:
                return {"dates": 0}
            out = {"dates": len(dates), "from": min(dates), "to": max(dates)}
            for st in ("buy", "wait"):
                n, s0, s60, ch, dr = (sum(cache[d][st][x] for d in dates) for x in range(5))
                out[st] = {"cards": n, "change_pct": round((s60 / s0 - 1) * 100, 1) if s0 else None,
                           "cheaper5_pct": round(ch * 100 / n) if n else None, "dearer5_pct": round(dr * 100 / n) if n else None}
            paid, now, low = (sum(cache[d][x] for d in dates) for x in ("paid", "now", "low"))
            out["overpay_following_pct"] = round((paid / low - 1) * 100, 1) if low else None
            out["overpay_buy_now_pct"] = round((now / low - 1) * 100, 1) if low else None
            n0 = sum(cache[d][st][1] for d in dates for st in ("buy", "wait"))
            n60 = sum(cache[d][st][2] for d in dates for st in ("buy", "wait"))
            out["market_change_pct"] = round((n60 / n0 - 1) * 100, 1) if n0 else None
            out["saved_pct"] = round((now - paid) / now * 100, 1) if now else None
            return out

        dates = sorted(cache)
        periods = []
        for y in sorted({d[:4] for d in dates}):
            for half, lo, hi in (("Jan–Jun", "01", "07"), ("Jul–Dec", "07", "13")):
                ds = [d for d in dates if d[:4] == y and lo <= d[5:7] < hi]
                if len(ds) >= 4:
                    sm = summary(ds)
                    periods.append({"label": f"{half} {y}", "dates": len(ds), "market_pct": sm["market_change_pct"],
                                    "buy_pct": sm["buy"]["change_pct"], "wait_pct": sm["wait"]["change_pct"],
                                    "saved_pct": round(sm["overpay_buy_now_pct"] - sm["overpay_following_pct"], 1)})
        return {"rule": RULE, "outlook_days": OUTLOOK, "all": summary(dates), "periods": periods,
                "live": summary([d for d in dates if d >= rule_since]), "rule_since": rule_since}


def spark(a, ords, i, step=14, span=365):
    """The last price in each two-week step over up to a year, oldest first, with the date of the first point."""
    pts, end = [], ords[i]
    while end > ords[i] - span:
        lo = bisect.bisect_left(ords, end - step + 1)
        hi = bisect.bisect_right(ords, end) - 1
        val = next((a[k] for k in range(min(hi, i), lo - 1, -1) if not math.isnan(a[k])), None)
        pts.append((end, None if val is None else round(val, 2)))
        end -= step
    pts.reverse()
    while pts and pts[0][1] is None:
        pts.pop(0)
    if not pts:
        return None, []
    return dt.date.fromordinal(pts[0][0]).isoformat(), [v for _, v in pts]


def booster_box(sealed, price_of):
    """Price of the set's booster box today, if it has one. price_of: {product id: price}."""
    best = None
    for pid, name in sealed.items():
        low = name.lower()
        if not any(w in low for w in SEALED_KEEP) or any(w in low for w in SEALED_SKIP):
            continue
        if pid in price_of and (best is None or len(name) < len(best[0])):
            best = (name, round(price_of[pid], 2))
    return best
