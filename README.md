# Pokemon TCG price drops

A daily report of Pokemon TCG card prices for completing master sets: which cards dropped, which are still falling (wait), and which have settled.

## How it works

- Every day at 21:40 UTC, GitHub Actions runs `tracker/track.py`.
- It pulls prices from [tcgcsv.com](https://tcgcsv.com), a free community mirror of TCGplayer, for every English set from Sword & Shield on. Each printing (Normal, Holofoil, Reverse Holofoil) is tracked separately.
- Today's prices are checked against the last good day. If a set fails, the feed looks partial, or prices look implausible, the run fails and saves nothing for that day. A GitHub issue is opened, and the day is filled in later from tcgcsv's archives.
- On the daily run only, up to `archives_per_run` days of missing history are loaded from tcgcsv's archives, gently. Pushing code does not download archives.
- It writes `report/latest.json` and `dashboard/index.html`.

## What the dashboard shows

- **Price check** (opens first): every card worth $1 or more, compared with its own last year of prices.
  - A bar from its lowest to its highest price, with a green good-price zone and a dot for today.
  - "Good price" is the level the card was at or below on only 20% of its days in the last year. At least 90 days of prices are needed.
  - Tapping a card shows its price chart, its typical and high prices, its label, and a link to TCGplayer.
  - Cards whose price never moves (few sales) are flagged rather than called a good price.
- **Track record**, shown at the top of the price check: on past dates, how often cards at a good price went another 10%+ lower within 60 days, compared with all cards. It is split by whether the card had steadied or was still falling. Computed from the history in `tracker/pricecheck.py` and cached in `data/pricecheck_scorecard.json`. Dates after the rule was fixed (`pricecheck_rule_since`) are also reported separately as live results.
- **Sets:** what every printing in each set costs at today's market prices, against the cost at good prices, and how many cards are at a good price. Tapping a set opens its cards in the price check.
- **Stable:** cards whose price has barely moved for a month.
- **Drops:** 7-day, 24-hour, below 30-day average, and new-low drops.
- **Labels on every card:**
  - Wait, too new
  - Wait, still falling
  - Wait, price spiking
  - Stable price
  - Some movement
  - Volatile

  The Wait labels were the most reliable in testing.

## Price history

`.github/workflows/history.yml` loads tcgcsv's archives back to their start (Feb 2024), about one every 30 seconds, 60 per run, three runs a day. Each day is downloaded once. Once the history is complete its runs finish in seconds, and the workflow can be disabled.

## Near Mint prices

With a free JustTCG API key saved as the repository secret `JUSTTCG_API_KEY`, each daily run refreshes Near Mint prices for about 600 cards (30 requests, under the free plan's monthly allowance). See `tracker/nearmint.py`. Each card gets its Near Mint price and JustTCG's Near Mint low and high over the past year. Cards at a good price are refreshed every 2 days, the rest weekly. Stored in `data/nearmint.json`.

## Limits

- The market price is one number per printing, not per condition. Treat it as the Near Mint price.
- The cheapest listing is for any condition, before shipping, and is usually a played copy.

## Checking the labels

`python tracker/backtest.py 30` scores past dates using only what was known then. It reports what prices did next, per label, including the best saving from waiting. Each label's gap to all cards comes with a 90% interval from resampling whole sets. By default it uses only dates outside the window the score was tuned on (Sep 8 – Nov 12, 2026); `--all` includes them.

## Tuning

Edit `config.json`:

- `min_price`: ignore cards cheaper than this.
- `thresholds`: minimum dollar and percent drop, and the card's own days of history, for each drop section.
- `max_rows_per_set`: cap per set in each drop section. Stable cards are never capped.
- `backfill_days`, `archives_per_run`: how far back history goes, and how much loads per daily run.
- `only_set_names_containing` / `exclude_set_names_containing`: narrow the sets tracked.
