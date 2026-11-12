"""Buy-now score: is this a good moment for a collector to buy this card?

0-100, built from four parts you can read off the card's own numbers:

  position  (35)  how cheap it is against its own last 90 days
  trend     (30)  has the price settled, or is it still falling / spiking
  stage     (20)  how far the set is into its print run: release date, plus
                  whether its sealed product is getting dearer (leaving print)
                  or cheaper (still plentiful)
  listing   (15)  is a copy actually listed at the market price

It describes where the price sits in its recent history. It does not predict
that a card cannot go lower.
"""

WEIGHTS = {"position": 35, "trend": 30, "stage": 20, "listing": 15}
MIN_POINTS = 21          # days of price history needed before scoring


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def price_near(series, age, slack=3):
    """Price about `age` days ago: the closest point between age and age+slack."""
    best = None
    for a, m in series:
        if age <= a <= age + slack and (best is None or a < best[0]):
            best = (a, m)
    return best[1] if best else None


def stage_score(months, undated):
    """Singles usually fall for the first months after release, sit near a
    floor while the set is in print, then climb after it leaves print."""
    if undated or months is None:
        return 0.6
    if months < 1:
        return 0.0
    if months < 3:
        return 0.1 + 0.5 * (months - 1) / 2      # 0.1 -> 0.6
    if months < 6:
        return 0.6 + 0.4 * (months - 3) / 3      # 0.6 -> 1.0
    if months < 18:
        return 1.0
    if months < 30:
        return 0.8
    return 0.65


SEALED_KEEP = ("booster box", "elite trainer box", "booster bundle", "booster pack")
SEALED_SKIP = ("case", "display", "pokemon center", "code card", "lot of", "half booster box")


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def supply_signal(items):
    """items: [(name, price_today, series)] for one set's sealed products.
    Rising sealed prices are the best free hint that a set is leaving print;
    falling ones mean product is still plentiful (or being reprinted)."""
    c30, clong, used = [], [], []
    for name, price, series in items:
        low = name.lower()
        if price < 3 or not any(w in low for w in SEALED_KEEP) or any(w in low for w in SEALED_SKIP):
            continue
        p30 = price_near(series, 28, slack=7)
        if not p30:
            continue
        c30.append(price / p30 - 1)
        old = [m for a, m in series if 75 <= a <= 93]
        if old:
            clong.append(price / (sum(old) / len(old)) - 1)
        used.append({"name": name, "price": round(price, 2), "chg30": round((price / p30 - 1) * 100, 1)})
    if not c30:
        return None
    m30 = median(c30)
    mlong = median(clong) if clong else None
    if m30 >= 0.06 or (mlong is not None and mlong >= 0.15 and m30 >= 0.02):
        state = "tightening"
    elif m30 <= -0.05 or (mlong is not None and mlong <= -0.12 and m30 <= -0.01):
        state = "plentiful"
    else:
        state = "steady"
    return {"state": state, "chg30": round(m30 * 100, 1),
            "chg90": None if mlong is None else round(mlong * 100, 1),
            "products": len(used), "items": sorted(used, key=lambda u: -u["price"])[:6]}


def score_card(price, listing, series, months, undated, buyable_within_pct=3, supply=None):
    """series: [(age_in_days, market_price)] for earlier days, any order.
    Returns None when there is not enough history."""
    hist = [(a, m) for a, m in series if 1 <= a <= 90]
    if len(hist) < MIN_POINTS:
        return None
    prices = [m for _, m in hist]
    avg90 = sum(prices) / len(prices)
    lo, hi = min(prices + [price]), max(prices + [price])
    below = (avg90 - price) / avg90 if avg90 else 0.0
    range_pos = 0.5 if hi - lo < 0.01 or (hi - lo) / hi < 0.03 else 1 - (price - lo) / (hi - lo)
    position = 0.6 * clamp(below / 0.30) + 0.4 * range_pos

    p7 = price_near(hist, 7)
    chg7 = (price - p7) / p7 if p7 else 0.0
    if chg7 < 0:      # still falling: waiting is cheaper
        trend = 1 - clamp((-chg7 - 0.02) / 0.10)
    else:             # spiking: likely hype, wait for it to cool
        trend = 1 - clamp((chg7 - 0.05) / 0.20)

    stage = stage_score(months, undated)
    tightening = bool(supply and supply["state"] == "tightening" and (months is None or months >= 4))
    if tightening:                       # set looks like it is leaving print: do not wait
        stage = 1.0
    elif supply and supply["state"] == "plentiful":
        stage *= 0.85                    # sealed still getting cheaper: singles can drift too

    if listing is None:
        avail = 0.3
    else:
        avail = 1 - clamp((listing / price - (1 + buyable_within_pct / 100)) / 0.20)

    parts = {"position": position, "trend": trend, "stage": stage, "listing": avail}
    total = round(sum(WEIGHTS[k] * v for k, v in parts.items()))

    # label: name the main blocker first, otherwise grade by score
    if not undated and months is not None and months < 2:
        label = "Wait, too new"
    elif chg7 <= -0.08:
        label = "Wait, still falling"
    elif chg7 >= 0.15:
        label = "Wait, price spiking"
    elif total >= 85 and below >= 0.10:   # cheap against its own history, settled, and available
        label = "Buy now"
    elif tightening and total >= 70 and below >= -0.05:
        label = "Buy soon, supply tightening"
    elif total >= 70:
        label = "Good price"
    else:
        label = "No rush"

    why = []
    if below >= 0.03:
        why.append(f"{round(below * 100)}% below its 90-day average")
    elif below <= -0.03:
        why.append(f"{round(-below * 100)}% above its 90-day average")
    else:
        why.append("at its 90-day average")
    if price <= lo * 1.02 and hi - lo >= 0.01 and (hi - lo) / hi >= 0.03:
        why.append("at its 90-day low")
    if chg7 <= -0.02:
        why.append(f"down {round(-chg7 * 100)}% this week")
    elif chg7 >= 0.05:
        why.append(f"up {round(chg7 * 100)}% this week")
    else:
        why.append("steady this week")
    if not undated and months is not None:
        why.append("set released this month" if months < 1 else f"set is {round(months)} months old")
    if supply and supply["state"] != "steady" and not (supply["state"] == "tightening" and not tightening):
        why.append(f"sealed product up {abs(round(supply['chg30']))}% in 30 days, supply tightening" if tightening
                   else f"sealed product down {abs(round(supply['chg30']))}% in 30 days, still plentiful")
    if listing is not None:
        gap = listing / price - 1
        why.append("a copy is listed at market" if gap <= buyable_within_pct / 100
                   else f"cheapest listing is {round(gap * 100)}% above market")

    return {"score": total, "label": label, "why": why,
            "avg90": round(avg90, 2), "chg7": round(chg7 * 100, 1),
            "parts": {k: round(WEIGHTS[k] * v) for k, v in parts.items()}}
