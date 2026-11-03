#!/usr/bin/env python3
"""Export the existing nflreadpy ESPN-to-GSIS crosswalk."""
import json
import math
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# update_nfl_data.py is imported as a module from its own directory.
sys.path.insert(0, str(SCRIPTS))

from update_nfl_data import fetch_id_crosswalk


def normalize_id(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None

    try:
        number = float(text)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))
    except ValueError:
        pass

    return text


import logging

with redirect_stdout(sys.stderr):
    logging.disable(logging.CRITICAL)
    try:
        df = fetch_id_crosswalk()
    finally:
        logging.disable(logging.NOTSET)

records = []
for row in df.to_dict(orient="records"):
    espn_id = normalize_id(row.get("espn_id"))
    gsis_id = normalize_id(row.get("gsis_id"))

    if espn_id and gsis_id:
        records.append({
            "espn_id": espn_id,
            "gsis_id": gsis_id,
        })

print(json.dumps(records))
