"""Turn the Dex payload copied from tools/dex_export.js into data/dex.json.

    python3 tools/dex_import.py PAYLOAD_FILE LENGTH HASH

LENGTH and HASH are what dex_export.js returned; the import refuses a copy that
doesn't match them, so a bad transcription can never overwrite good data.
"""
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "dex.json"


def main():
    path, length, want = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    text = Path(path).read_text().strip("\n")
    h = 0
    for ch in text:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    if len(text) != length or h != want:
        sys.exit(f"REFUSED: copy does not match Dex (length {len(text)} vs {length}, hash {h} vs {want})")
    head, _, body = text.partition("\n#\n")
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    sets = {s["id"]: s for s in old.get("sets", [])}
    for line in filter(None, head.split("\n")):
        sid, name, play, typ, own, tot = (line.split("|") + [""] * 6)[:6]
        sets[sid] = {"id": sid, "name": name, "play": play, "type": typ,
                     "owned": int(own) if own.isdigit() else None, "total": int(tot) if tot.isdigit() else None}
    owned = {}
    for part in filter(None, body.split("|")):
        sid, cards = part.split("=", 1)
        owned[sid] = cards
    if not owned:
        sys.exit("REFUSED: no owned cards in the copy (signed out?)")
    n_new = sum(len(v.split(",")) for v in owned.values())
    n_old = sum(len(v.split(",")) for v in old.get("owned", {}).values())
    if n_old and n_new < 0.5 * n_old:
        sys.exit(f"REFUSED: owned cards dropped from {n_old} to {n_new}; looks like a partial sync")
    missing_meta = [s for s in owned if s not in sets]
    out = {"user": "kushp1", "fetched": dt.date.today().isoformat(),
           "sets": [sets[s] for s in sorted(sets) if s in owned], "owned": dict(sorted(owned.items()))}
    if old.get("owned") == out["owned"] and old.get("sets") == out["sets"]:
        print(f"unchanged: {n_new} owned cards in {len(owned)} sets")
        return
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"updated: {n_new} owned cards in {len(owned)} sets (was {n_old})"
          + (f"; no set details for {missing_meta}" if missing_meta else ""))


if __name__ == "__main__":
    main()
