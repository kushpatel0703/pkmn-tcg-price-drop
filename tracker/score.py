"""Buy-now score: is this a good moment for a collector to buy this card?

0-100, built from four parts you can read off the card's own numbers:

  position  (35)  how cheap it is against its own last 90 days
  trend     (30)  has the price settled, or is it still falling / spiking
  stage     (20)  how far the set is into its print run (by release date)
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


def score_card(price, listing, series, months, undated, buyable_within_pct=3):
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
    if listing is not None:
        gap = listing / price - 1
        why.append("a copy is listed at market" if gap <= buyable_within_pct / 100
                   else f"cheapest listing is {round(gap * 100)}% above market")

    return {"score": total, "label": label, "why": why,
            "avg90": round(avg90, 2), "chg7": round(chg7 * 100, 1),
            "parts": {k: round(WEIGHTS[k] * v) for k, v in parts.items()}}
