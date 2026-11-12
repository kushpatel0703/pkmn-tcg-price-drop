"""Buy-now score: is waiting likely to get a collector a better price?

0-100. A high score means the price looks settled, so waiting is unlikely to
save money. Built from three parts read off the card's own numbers:

  settled   (55)  has the price been flat for the last 30 days (not falling,
                  not spiking)
  stage     (30)  how far the set is into its print run: release date, plus
                  whether its sealed product is getting dearer (leaving print)
                  or cheaper (still plentiful)
  listing   (15)  is a copy listed close to the market price

A discount against the 90-day average earns no points. In the first backtest
(Sep-Nov 2026, a falling market) cards that were cheap against their average
kept falling; a 30-day flat price was the best sign that waiting would not
help. The discount is still reported so you can see it. See tracker/backtest.py.

It describes the price history. It does not predict that a card cannot go lower.
"""

WEIGHTS = {"settled": 55, "stage": 30, "listing": 15}
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
    """Singles fall hardest in the first months after release and, in the
    history tested so far, kept drifting down until the set was a few years old."""
    if undated or months is None:
        return 0.6
    if months < 2:
        return 0.0
    if months < 6:
        return 0.2
    if months < 12:
        return 0.4
    if months < 30:
        return 0.6
    return 1.0


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
    if len(c30) < 2:                     # one product is too thin to call a trend
        state = "steady"
    elif m30 >= 0.06 or (mlong is not None and mlong >= 0.15 and m30 >= 0.02):
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

    p7 = price_near(hist, 7)
    p30 = price_near(hist, 28, slack=7)
    chg7 = (price - p7) / p7 if p7 else 0.0
    chg30 = (price - p30) / p30 if p30 else chg7
    last30 = [m for a, m in hist if a <= 30] + [price]
    rng30 = (max(last30) - min(last30)) / max(last30)
    # flat over 30 days, no drift over the month, quiet this week
    settled = (0.5 * (1 - clamp((rng30 - 0.03) / 0.17))
               + 0.3 * (1 - clamp((abs(chg30) - 0.03) / 0.12))
               + 0.2 * (1 - clamp((abs(chg7) - 0.02) / 0.08)))

    stage = stage_score(months, undated)
    tightening = bool(supply and supply["state"] == "tightening" and (months is None or months >= 4))
    if tightening:                       # set looks like it is leaving print: do not wait
        stage = 1.0
    elif supply and supply["state"] == "plentiful":
        stage *= 0.7                     # sealed still getting cheaper: singles can drift too

    if listing is None:
        avail = 0.3
    else:
        gap = listing / price - 1
        if gap > buyable_within_pct / 100:     # nothing listed at market: harder to buy at this price
            avail = 1 - clamp((gap - buyable_within_pct / 100) / 0.15)
        else:                                  # listings far under market usually pull the market price down
            avail = 1 - 0.7 * clamp((-gap - 0.10) / 0.30)

    parts = {"settled": settled, "stage": stage, "listing": avail}
    total = round(sum(WEIGHTS[k] * v for k, v in parts.items()))

    # label: name the main blocker first, otherwise grade by score
    if not undated and months is not None and months < 3:
        label = "Wait, too new"
    elif chg7 <= -0.08 or chg30 <= -0.10:
        label = "Wait, still falling"
    elif chg7 >= 0.08 or chg30 >= 0.20:
        label = "Wait, price spiking"
    elif total >= 85:
        label = "Buy now"
    elif tightening and total >= 65:
        label = "Buy soon, supply tightening"
    elif total >= 65:
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
    if chg30 <= -0.03:
        why.append(f"down {round(-chg30 * 100)}% in 30 days")
    elif chg30 >= 0.03:
        why.append(f"up {round(chg30 * 100)}% in 30 days")
    else:
        why.append("flat for 30 days" if rng30 <= 0.06 else "about level over 30 days")
    if chg7 <= -0.02:
        why.append(f"down {round(-chg7 * 100)}% this week")
    elif chg7 >= 0.02:
        why.append(f"up {round(chg7 * 100)}% this week")
    if not undated and months is not None:
        why.append("set released this month" if months < 1 else f"set is {round(months)} months old")
    if supply and supply["state"] != "steady" and not (supply["state"] == "tightening" and not tightening):
        # quote whichever window shows the move more clearly
        c30, c90 = supply["chg30"], supply.get("chg90")
        pct, span = (c90, "90 days") if c90 is not None and abs(c90) > abs(c30) else (c30, "30 days")
        why.append(f"sealed product up {abs(round(pct))}% in {span}, supply tightening" if tightening
                   else f"sealed product down {abs(round(pct))}% in {span}, still plentiful")
    if listing is not None:
        gap = listing / price - 1
        why.append("a copy is listed at market" if gap <= buyable_within_pct / 100
                   else f"cheapest listing is {round(gap * 100)}% above market")

    return {"score": total, "label": label, "why": why,
            "avg90": round(avg90, 2), "chg7": round(chg7 * 100, 1), "chg30": round(chg30 * 100, 1),
            "parts": {k: round(WEIGHTS[k] * v) for k, v in parts.items()}}
