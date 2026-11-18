# Pokemon TCG: buy first or wait

A daily tool for master-set collectors buying raw Near Mint singles on TCGplayer. For each set and each card worth $1 or more, it says **Buy first** or **Can wait**, with one reason. Read it as priority: Buy-first cards have kept rising after being labelled; can-wait cards rose less or fell, so they carry less risk if you put them off. Can wait does not promise a lower price later.

## How it works

- Every day at 21:40 UTC, GitHub Actions runs `tracker/track.py`.
- Prices come from [tcgcsv.com](https://tcgcsv.com), a free daily mirror of TCGplayer. Every English set from Sword & Shield on is covered, each printing separately. Today's prices are checked against the last good day; partial or implausible data fails the run (a GitHub issue opens) and that day is filled in later from tcgcsv's archives.
- `tracker/buywait.py` labels every card each day, using only data up to that day.
- The run writes `report/latest.json` and `dashboard/index.html`. A morning task republishes the dashboard and sends a phone summary of what changed.

## Buy first / Can wait

A card gets **Can wait** when the first of these applies:

| Reason | Rule |
|---|---|
| New set | Released under 6 months ago |
| Just listed | First priced under 45 days ago |
| Set falling | The median card in the set is down 3%+ over 8 weeks. It stays falling until that recovers above −1%. |
| Still dropping | Down 8%+ in a week, or 10%+ in 4 weeks |
| Just jumped | Up 8%+ in a week, or 20%+ in 4 weeks |

Otherwise it gets **Buy first**, once nothing has flagged it for 7 days. Prices from before a set's release are ignored.

**Market-aware:** when the median card across all sets is up 2%+ over 8 weeks, only *Still dropping* holds a card back. In rising markets waiting on new, falling or jumped cards cost money. This switch was chosen on 2024–25 data and then checked on 2026, which it was not tuned on: following the labels saved 1.6% vs buying at once in 2026, against 0.8% without it. Turn it off with `"market_aware": false` in `config.json`.

Why it is set-led: in the 2026 backtests, a card being at its own low did not predict that waiting would pay; those cards kept falling. A falling or new set did predict it.

The **track record** at the top of the dashboard is recomputed every run from the history on file. On weekly past dates it compares what Buy-first and can-wait cards did over the next 60 days, and what following the labels paid versus buying everything straight away, overall and per half-year (so a single blended number can't hide a bad period). With history from Apr 2024: Buy-first cards rose more than can-wait cards in 5 of 6 half-years, and following the labels saved about 1.6% overall. Cached in `data/buywait_evidence.json`; the cache resets when the rule or the start of the history changes. Dates after `buywait_rule_since` are also reported separately as live results.

## Dashboard

- **Sets:** each set's state (new, falling, or steady, with its 8-week change), the number and dollar value of cards that are Buy first versus Can wait, by group:
  - Chase ($20+)
  - Mid ($1–20)
  - Ball patterns
  - Bulk (under $1; buy any time, in one order)

  Also shows the booster box price for comparison. Buttons copy a Buy-first list or bulk list in TCGplayer Mass Entry format (`1 Name [SET] number`); choose Near Mint and the printing in Mass Entry, then use the cart optimizer.
- **Cards:** every card $1+ with Buy first / Can wait and its reason. Tap a card for its price chart with dates, its low and high since our history starts, the Near Mint 1-year range from JustTCG, and a TCGplayer link. Filters and "my sets" are remembered on the device.

## Your Dex collection

`data/dex.json` holds the cards you own in [Dex](https://dextcg.com) (@kushp1): for each Dex set, the card numbers and which of Normal / Reverse Holo / Holo you have. Dex sets are matched to TCGplayer sets by TCGplayer's set code (or name).

- Cards you own are hidden everywhere: card lists, set totals, copy lists.
- **Cards I need** (the default when the file exists) shows only your Dex sets, with Dex progress and what's left to buy. **Good value now** is the buy-first filter; together they answer "what should I buy today".
- **Budget** tab: enter an amount and it builds an order from the cards you need: buy-first cards first (most expensive first, each only if it still fits), then whole-set bulk lists (sets already in the order first, then the cheapest; budgeted at TCGplayer Direct's $0.40-per-card minimum), and optionally can-wait cards within 10% of their 1-year low. Prices are market prices before shipping and tax.
- Copy buttons give one TCGplayer Mass Entry list per printing (normal / reverse holo / holo), since Mass Entry can't take the printing per line.
- Ball patterns and other printings Dex doesn't track count as not owned.

**Refreshing it.** Dex blocks requests from GitHub's servers, so the list is read from your signed-in Dex web page in Chrome: `tools/dex_export.js` reads the owned list Dex keeps in the browser (read-only) and returns a length and hash; `tools/dex_import.py PAYLOAD LENGTH HASH` writes `data/dex.json` only if the copy matches exactly, and refuses a list that shrank by half (a partial sync or signed-out page). A scheduled Claude task does this daily when your Mac and Chrome are on; you can also ask Claude to "refresh my Dex". Committing `data/dex.json` triggers a dashboard rebuild.

## Data

- `data/snapshots/`: one file per day (market price and cheapest any-condition listing per printing).
- `.github/workflows/history.yml` loads tcgcsv's archives back to Feb 2024, gently, restarting hourly until done.
- `data/nearmint.json`: JustTCG Near Mint 1-year low/high, about 600 cards a day.
  - Needs the repository secret `JUSTTCG_API_KEY`.
  - Its Near Mint price matches TCGplayer's market price closely, so only the 1-year range is shown.

## Limits

- Backtested on Apr 2024 – Sep 2026. Savings are small (a few percent); the main value is ordering purchases, not timing the bottom.
- The market-aware switch reacts to an 8-week trend, so it lags turns by a few weeks.
- Market price is not split by condition; treat it as the Near Mint price.
