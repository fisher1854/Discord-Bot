"""Queue one Isle command as a unique inbox JSON file.

On the game host this lands at:
  TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/inbox/<id>.json
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inbox-dir", required=True, help="Path to Saved/inbox on the game server")
    parser.add_argument("--verb", required=True, choices=("store", "redeem", "storeinfo", "primeinfo", "tpinfo"))
    parser.add_argument("--steam", required=True, help="SteamID64")
    parser.add_argument("--slot", default="", help="For redeem: vault slot id")
    args = parser.parse_args()

    job_id = uuid.uuid4().hex[:12]
    line = {
        "id": job_id,
        "ts": int(time.time()),
        "verb": args.verb,
        "steam": args.steam.strip(),
    }
    if args.slot:
        line["slot"] = args.slot
    path = Path(args.inbox_dir)
    path.mkdir(parents=True, exist_ok=True)
    dest = path / (job_id + ".json")
    dest.write_text(json.dumps(line, separators=(",", ":")) + "\n", encoding="utf-8")
    print("queued", job_id, args.verb, args.steam, "->", dest)


if __name__ == "__main__":
    main()
