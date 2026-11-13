"""Near Mint prices from JustTCG (justtcg.com), kept in data/nearmint.json.

The free plan allows about 1,000 requests a month (100 a day) at 20 cards per
request, so each daily run refreshes a slice: cards never checked first, then
cards at a good price older than 2 days, then the rest older than 7 days.
About 600 cards a day, so every card is refreshed roughly weekly.

For each card printing it stores the Near Mint price and JustTCG's Near Mint
low, high and 30-day average over the last year. Needs the JUSTTCG_API_KEY
secret; without it nothing happens.
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.justtcg.com/v1/cards"
BATCH = 20                 # free-plan batch size
REQUESTS_PER_RUN = 30      # ~900 a month, under the free plan's 1,000
KEEP_SPARE = 60            # stop when the plan has this few requests left this month
STALE_AT = 2               # days before a card at a good price is checked again
STALE_OTHER = 7


def _post(key, items):
    req = urllib.request.Request(API, data=json.dumps(items).encode(), method="POST",
                                 headers={"x-api-key": key, "Content-Type": "application/json",
                                          "User-Agent": "pkmn-tcg-price-drop/1.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read(5 * 2**20))


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def refresh(path, pc_cards, today, key):
    """pc_cards: the price-check rows (id, printing, price, at). Updates path in place."""
    key = (key or "").strip()
    if not key or os.environ.get("SKIP_BACKFILL"):
        return
    store = load(path)
    t = dt.date.fromisoformat(today)

    def age(k):
        e = store.get(k)
        return None if not e else (t - dt.date.fromisoformat(e["checked"])).days

    due = []
    for c in pc_cards:
        k = f"{c['id']}|{c['printing']}"
        a = age(k)
        if a is None or a >= (STALE_AT if c["at"] else STALE_OTHER):
            due.append((a is not None, not c["at"], -c["price"], c["id"]))
    due.sort()
    ids = []
    for _, _, _, pid in due:                 # one request covers every printing of a card
        if str(pid) not in ids:
            ids.append(str(pid))
    ids = ids[:BATCH * REQUESTS_PER_RUN]
    if not ids:
        return
    done, used = 0, 0
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        try:
            out = _post(key, [{"tcgplayerId": pid, "condition": "Near Mint", "include_statistics": "1y,30d"}
                              for pid in chunk])
        except urllib.error.HTTPError as e:
            print(f"near mint: JustTCG answered {e.code}; stopping for today")
            break
        except Exception as e:
            print(f"near mint: request failed ({e}); stopping for today")
            break
        used += 1
        for card in out.get("data") or []:
            pid = str(card.get("tcgplayerId") or "")
            for v in card.get("variants") or []:
                if (v.get("condition") or "").lower() != "near mint":
                    continue
                k = f"{pid}|{v.get('printing')}"
                price = v.get("price")
                store[k] = {"checked": today,
                            "nm": round(price, 2) if isinstance(price, (int, float)) else None,
                            "status": v.get("priceStatus"),
                            "low1y": v.get("minPrice1y"), "high1y": v.get("maxPrice1y"),
                            "avg30": v.get("avgPrice30d")}
                done += 1
        meta = out.get("_metadata") or {}
        if (meta.get("apiDailyRequestsRemaining") or 99) <= 5 or (meta.get("apiRequestsRemaining") or 999) <= KEEP_SPARE:
            print("near mint: plan allowance nearly used; stopping for today")
            break
        time.sleep(7)                        # the free plan allows 10 requests a minute
    path.write_text(json.dumps(store, separators=(",", ":"), sort_keys=True))
    print(f"near mint: {done} printings refreshed with {used} requests; {len(store)} on file")


def annotate(path, pc_cards, today):
    """Add Near Mint fields to each price-check row that has them. Returns how many."""
    store = load(path)
    t = dt.date.fromisoformat(today)
    n = 0
    for c in pc_cards:
        e = store.get(f"{c['id']}|{c['printing']}")
        if not e or e.get("nm") is None:
            continue
        c["nm"] = e["nm"]
        c["nm_age"] = (t - dt.date.fromisoformat(e["checked"])).days
        if isinstance(e.get("low1y"), (int, float)) and isinstance(e.get("high1y"), (int, float)):
            c["nm_low1y"], c["nm_high1y"] = round(e["low1y"], 2), round(e["high1y"], 2)
        c["nm_at"] = e["nm"] <= c["target"]
        n += 1
    return n
