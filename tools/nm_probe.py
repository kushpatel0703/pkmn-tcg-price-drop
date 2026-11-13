#!/usr/bin/env python3
"""One-off check of JustTCG's Near Mint prices for 20 tracked cards.
Reads the API key from the JUSTTCG_API_KEY environment variable and never prints it."""
import json, os, sys, urllib.request, urllib.error
from pathlib import Path

key = os.environ.get("JUSTTCG_API_KEY", "").strip()
if not key:
    sys.exit("JUSTTCG_API_KEY is not set (add it under Settings > Secrets and variables > Actions)")
report = json.loads((Path(__file__).resolve().parent.parent / "report" / "latest.json").read_text())
cards = sorted(report["pricecheck"]["cards"], key=lambda c: -c["price"])
# a spread of prices and printings
pick = [cards[i] for i in range(0, len(cards), max(1, len(cards) // 20))][:20]
body = [{"tcgplayerId": str(c["id"]), "condition": "Near Mint", "include_statistics": "1y"} for c in pick]
req = urllib.request.Request("https://api.justtcg.com/v1/cards", data=json.dumps(body).encode(), method="POST",
                             headers={"x-api-key": key, "Content-Type": "application/json",
                                      "User-Agent": "pkmn-tcg-price-drop/1.1"})
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        status, out = r.status, json.loads(r.read())
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read()[:500].decode(errors="replace"))
    sys.exit(1)
print("HTTP", status, "| usage:", json.dumps(out.get("_metadata") or out.get("usage") or {}))
print("top-level keys:", list(out))
data = out.get("data") or []
print("cards returned:", len(data), "of", len(pick))
by_id = {str(c["id"]): c for c in pick}
for card in data:
    ours = by_id.get(str(card.get("tcgplayerId")))
    print("\n##", card.get("name"), "|", card.get("set_name") or card.get("set"), "| tcgplayerId", card.get("tcgplayerId"))
    for v in card.get("variants", []):
        mine = ours and ours["printing"] == v.get("printing")
        print(f"  {v.get('printing'):<18} {v.get('condition'):<12} price={v.get('price')} status={v.get('priceStatus')} "
              f"min1y={v.get('minPrice1y')} max1y={v.get('maxPrice1y')} avg30={v.get('avgPrice30d')}"
              + (f"   <- ours: market {ours['price']}, good<= {ours['target']}, low {ours['low']}" if mine else ""))
if data:
    print("\nvariant fields:", sorted(data[0]["variants"][0].keys()) if data[0].get("variants") else "none")
