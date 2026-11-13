# Pokemon TCG: buy or wait

A daily tool for master-set collectors buying raw Near Mint singles on TCGplayer. For each set and each card worth $1 or more, it says **Buy** (waiting has saved little on cards like this) or **Wait** (waiting has usually paid), with one reason.

## How it works

- Every day at 21:40 UTC, GitHub Actions runs `tracker/track.py`.
- Prices come from [tcgcsv.com](https://tcgcsv.com), a free daily mirror of TCGplayer. Every English set from Sword & Shield on is covered, each printing separately. Today's prices are checked against the last good day; partial or implausible data fails the run (a GitHub issue opens) and that day is filled in later from tcgcsv's archives.
- `tracker/buywait.py` labels every card each day, using only data up to that day.
- The run writes `report/latest.json` and `dashboard/index.html`. A morning task republishes the dashboard and sends a phone summary of what changed.

## Buy / Wait

A card gets **Wait** when the first of these applies:

| Reason | Rule |
|---|---|
| New set | Released under 6 months ago |
| Just listed | First priced under 45 days ago |
| Set falling | The median card in the set is down 3%+ over 8 weeks. It stays falling until that recovers above −1%. |
| Still dropping | Down 8%+ in a week, or 10%+ in 4 weeks |
| Just jumped | Up 8%+ in a week, or 20%+ in 4 weeks |

Otherwise it gets **Buy**, once nothing has flagged it for 7 days. Prices from before a set's release are ignored.

Why it is set-led: in the 2026 backtests, a card being at its own low did not predict that waiting would pay; those cards kept falling. A falling or new set did predict it.

The **track record** at the top of the dashboard is recomputed every run from the history on file. On weekly past dates it compares what cards labelled Buy and Wait did over the next 60 days, and what following the labels paid against each card's 60-day low, versus buying straight away. Cached in `data/buywait_evidence.json`; the cache resets when the rule or the start of the history changes. Dates after `buywait_rule_since` are also reported separately as live results.

## Dashboard

- **Sets:** each set's state (new, falling, or steady, with its 8-week change), the number and dollar value of cards that are Buy now versus Wait, by group:
  - Chase ($20+)
  - Mid ($1–20)
  - Ball patterns
  - Bulk (under $1; buy any time, in one order)

  Also shows the booster box price for comparison. Buttons copy a Buy list or bulk list in TCGplayer Mass Entry format (`1 Name [SET] number`); choose Near Mint and the printing in Mass Entry, then use the cart optimizer.
- **Cards:** every card $1+ with Buy/Wait and its reason. Tap a card for its price chart with dates, its low and high since our history starts, the Near Mint 1-year range from JustTCG, and a TCGplayer link. Filters and "my sets" are remembered on the device.

## Data

- `data/snapshots/`: one file per day (market price and cheapest any-condition listing per printing).
- `.github/workflows/history.yml` loads tcgcsv's archives back to Feb 2024, gently, restarting hourly until done.
- `data/nearmint.json`: JustTCG Near Mint 1-year low/high, about 600 cards a day.
  - Needs the repository secret `JUSTTCG_API_KEY`.
  - Its Near Mint price matches TCGplayer's market price closely, so only the 1-year range is shown.

## Limits

- Tested only on 2026 prices, mostly a falling market. In a rising market waiting costs money and Wait would lag by a few weeks.
- Market price is not split by condition; treat it as the Near Mint price.
