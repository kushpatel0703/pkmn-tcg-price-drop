# Pokemon TCG price drops

A daily report of Pokemon TCG cards whose TCGplayer market price has dropped the most, for completing master sets as cheaply as possible.

## How it works

- Every day at 21:40 UTC, GitHub Actions runs `tracker/track.py`.
- It pulls prices from [tcgcsv.com](https://tcgcsv.com) (a free daily mirror of TCGplayer) for every English set released since February 2020 (Sword & Shield onward), one row per printing (Normal, Holofoil, Reverse Holofoil).
- Each day's prices are saved to `data/snapshots/`. The first run loads the previous 90 days from tcgcsv's archive.
- It writes `report/latest.json` and `dashboard/index.html` with four sections:
  - **24 hours**: biggest drops since the previous day
  - **7 days**: biggest drops over a week
  - **vs 30-day avg**: cards furthest below their 30-day average
  - **New lows**: cards below their lowest price since tracking began

## Tuning

Edit `config.json`:

- `min_price`: ignore cards cheaper than this
- `thresholds`: minimum dollar and percent drop for each section
- `only_set_names_containing` / `exclude_set_names_containing`: narrow the sets tracked, e.g. `["Evolving Skies", "151"]`
- `sets_published_on_or_after`: earliest set release date to track
