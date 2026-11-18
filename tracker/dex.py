"""Your Dex collection (data/dex.json, see tools/dex_import.py): which printings
you own, matched to TCGplayer sets and printings.

Dex sets are matched to TCGplayer sets by TCGplayer's set code (Dex's playId,
e.g. DRI) or, failing that, by name. Dex tracks Normal, Reverse Holo and Holo;
other TCGplayer printings (ball patterns and the like) count as not owned."""
import json
import re
from pathlib import Path

LETTER = {"Normal": "n", "Reverse Holofoil": "r", "Holofoil": "h"}


def _norm(name):
    name = re.sub(r"^[A-Za-z0-9&.\- ]{1,12}:\s*", "", name)   # "SV10: Destined Rivals" -> "Destined Rivals"
    return re.sub(r"[^a-z0-9]", "", name.lower())


def card_number(num):
    """TCGplayer '022/182' or 'TG04/TG30' -> Dex '22' / 'TG04'."""
    n = (num or "").split("/")[0].strip().upper()
    return n.lstrip("0") or "0" if n.isdigit() else n


class Collection:
    def __init__(self, path, groups, abbr):
        self.ok = False
        self.sets, self.unmatched, self.fetched = {}, [], None
        try:
            d = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return
        by_abbr = {a.upper(): n for n, a in abbr.items() if a}
        by_name = {_norm(g["name"]): g["name"] for g in groups}
        self.owned = {}            # TCGplayer set name -> {number: letters}
        for s in d.get("sets", []):
            g = by_abbr.get((s.get("play") or "").upper()) or by_name.get(_norm(s.get("name", "")))
            cards = d.get("owned", {}).get(s["id"])
            if not g or not cards:
                if cards:
                    self.unmatched.append(s.get("name") or s["id"])
                continue
            have = {}
            for e in cards.split(","):
                m = re.match(r"(.+?)([hnr]+)$", e)
                if m:
                    have[card_number(m.group(1))] = set(m.group(2))
            self.owned[g] = have
            self.sets[g] = {"dex_id": s["id"], "dex_owned": s.get("owned"), "dex_total": s.get("total")}
        self.fetched = d.get("fetched")
        self.ok = bool(self.owned)

    def mine(self, set_name):
        return set_name in self.owned

    def owns(self, set_name, card, printing):
        have = self.owned.get(set_name)
        if have is None:
            return False
        name = (card.get("name") or "").lower()
        if "ball pattern" in name or "master ball" in name or "poke ball" in name:
            return False
        letter = LETTER.get(printing)
        return bool(letter) and letter in have.get(card_number(card.get("num")), ())
