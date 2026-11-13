"""Price-stability score and wait signals for each card.

What this can and cannot tell you (from tracker/backtest.py, Aug-Nov 2026):

- The "Wait" labels held up: cards from sets under 3 months old, cards still
  falling, and cards spiking all got cheaper than average over the next month.
  This is the most reliable part, so it comes first.
- The 0-100 score measures how quiet a card's price has been over the last
  30 days. Quiet cards fell less than others in a falling market, but they
  still fell on average: it is NOT evidence that buying now beats waiting.
  An independent review found an earlier "buy now" score did no better than
  this simple quietness measure, so the score is now exactly that.

"Reasonable time to buy" (checked after the Wait labels, so never a card
that is new, still falling or spiking): the price is in the cheapest quarter of
the card's own history (60+ days of it) and has stopped sliding for two weeks.
It says the price is low for this card and not currently dropping. It does not
say the price cannot go lower; see tracker/backtest.py for how such cards did.

It describes price history. It does not predict prices.
"""

MIN_POINTS = 21          # days of price history needed before scoring
# "Reasonable time to buy": set before testing, not tuned on outcomes
BUY_MIN_HISTORY = 60     # days of the card's own prices needed
BUY_MAX_PCTILE = 0.25    # today's price is in the cheapest quarter of the card's own history
BUY_MAX_FALL_14 = -0.03  # not down more than 3% over the last two weeks (the slide has paused)
BUY_MAX_FALL_7 = -0.02   # nor more than 2% this week
NEW_CARD_DAYS = 45       # a printing first seen this recently counts as new


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def price_near(series, age, slack=3):
    """Price about `age` days ago: the closest point between age and age+slack."""
    best = None
    for a, m in series:
        if age <= a <= age + slack and (best is None or a < best[0]):
            best = (a, m)
    return best[1] if best else None


SEALED_KEEP = ("booster box", "elite trainer box", "booster bundle", "booster pack")
SEALED_SKIP = ("case", "display", "pokemon center", "code card", "lot of", "half booster box")


def supply_signal(items):
    """items: [(name, price_today, series)] for one set's sealed products.
    Shown as information only: the backtest found no reliable link between a
    'rising' state and later singles prices (it was mostly seasonal Halloween
    bundles), so it does not change any label."""
    c30, clong = [], []
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
    if len(c30) < 2:                     # one product is too thin to call a trend
        return None
    m30 = median(c30)
    mlong = median(clong) if clong else None
    state = "rising" if m30 >= 0.06 else "falling" if m30 <= -0.05 else "steady"
    return {"state": state, "chg30": round(m30 * 100, 1),
            "chg90": None if mlong is None else round(mlong * 100, 1), "products": len(c30)}


def score_card(price, series, months, undated, card_age_days=None, supply=None, full_history=None):
    """series: [(age_in_days, market_price)] for earlier days, any order.
    months: set age in months (None if unknown); undated: promo-style set
    whose release date says nothing about the card; card_age_days: days since
    this printing was first seen (None if it predates the price history);
    full_history: every earlier market price on file for this card, for the
    "Reasonable time to buy" check. Returns None when there is not enough history."""
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
    chg30 = (price - p30) / p30 if p30 else None
    last30 = [m for a, m in hist if a <= 30] + [price]
    rng30 = (max(last30) - min(last30)) / max(last30)
    # flat over 30 days, no drift over the month, quiet this week
    stability = (0.5 * (1 - clamp((rng30 - 0.03) / 0.17))
                 + 0.3 * (1 - clamp((abs(chg30 if chg30 is not None else chg7) - 0.03) / 0.12))
                 + 0.2 * (1 - clamp((abs(chg7) - 0.02) / 0.08)))
    score = round(100 * stability)

    new_set = not undated and months is not None and months < 3
    new_card = card_age_days is not None and card_age_days < NEW_CARD_DAYS
    p14 = price_near(hist, 14)
    chg14 = (price - p14) / p14 if p14 else chg7
    allp = list(full_history) if full_history else prices
    # mid-rank, so a price that never moves sits at the middle, not the bottom
    below_n = sum(1 for x in allp if x < price)
    equal_n = sum(1 for x in allp if x == price)
    pctile = (below_n + 0.5 * equal_n) / len(allp)
    dearer_share = sum(1 for x in allp if x > price) / len(allp)
    reasonable = (len(allp) >= BUY_MIN_HISTORY and pctile <= BUY_MAX_PCTILE
                  and chg14 > BUY_MAX_FALL_14 and chg7 > BUY_MAX_FALL_7)
    if new_set or new_card:
        label = "Wait, too new"
    elif chg7 <= -0.08 or (chg30 is not None and chg30 <= -0.10):
        label = "Wait, still falling"
    elif chg7 >= 0.08 or (chg30 is not None and chg30 >= 0.20):
        label = "Wait, price spiking"
    elif reasonable:
        label = "Reasonable time to buy"
    elif score >= 85:
        label = "Stable price"
    elif score >= 60:
        label = "Some movement"
    else:
        label = "Volatile"

    why = []
    if label == "Reasonable time to buy":
        why.append(f"cheaper than on {round(dearer_share * 100)}% of its last {len(allp)} days")
        why.append("held steady for 2 weeks" if abs(chg14) < 0.02 else
                   f"{'up' if chg14 > 0 else 'down'} {abs(round(chg14 * 100))}% over 2 weeks")
    if new_card and not new_set:
        why.append(f"first priced {card_age_days} days ago")
    if below >= 0.03:
        why.append(f"{round(below * 100)}% below its 90-day average")
    elif below <= -0.03:
        why.append(f"{round(-below * 100)}% above its 90-day average")
    else:
        why.append("at its 90-day average")
    if price <= lo * 1.02 and hi - lo >= 0.01 and (hi - lo) / hi >= 0.03:
        why.append("at its 90-day low")
    if chg30 is None:
        pass                              # no price from a month ago: say nothing about 30 days
    elif chg30 <= -0.03:
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
    if supply and supply["state"] != "steady":
        why.append(f"set's sealed product {'up' if supply['chg30'] > 0 else 'down'} "
                   f"{abs(round(supply['chg30']))}% in 30 days")

    return {"score": score, "label": label, "why": why, "avg90": round(avg90, 2),
            "chg7": round(chg7 * 100, 1), "chg30": None if chg30 is None else round(chg30 * 100, 1),
            "range30": round(rng30 * 100, 1), "pctile": round(pctile * 100), "hist_days": len(allp)}
