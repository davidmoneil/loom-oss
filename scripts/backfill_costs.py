#!/usr/bin/env python3
"""Recompute cost_estimate for metrics rows stored at $0 whose model now has a price.

Dry run by default: prints per-model totals and changes nothing. Pass --apply
to write. Only rows with cost_estimate = 0 (or NULL) and a status below 400 are
touched, so rows that already carry a cost are never changed.

Old rows don't record the 5-minute / 1-hour cache-write split, so every cache
write is priced as 5-minute (1.25x input). If clients used the 1-hour TTL the
real figure is higher.

Usage:
    python scripts/backfill_costs.py [--config loom.yaml] [--since-days N] [--apply]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loom import pricing  # noqa: E402
from loom.config import LoomConfig  # noqa: E402
from loom.storage import create_storage  # noqa: E402

SELECT = """
    SELECT id, model, tokens_in, tokens_out,
           COALESCE(cache_read_tokens, 0), COALESCE(cache_creation_tokens, 0)
    FROM metrics
    WHERE COALESCE(cost_estimate, 0) = 0
      AND COALESCE(status_code, 200) < 400
      AND timestamp >= {ph}
"""
UPDATE = "UPDATE metrics SET cost_estimate = {ph} WHERE id = {ph} AND COALESCE(cost_estimate, 0) = 0"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", help="loom.yaml path (default: normal Loom lookup)")
    ap.add_argument("--since-days", type=float, default=0, help="only rows this recent (0 = all)")
    ap.add_argument("--apply", action="store_true", help="write the new costs")
    args = ap.parse_args()

    config = LoomConfig.from_yaml(args.config) if args.config else LoomConfig.load()
    storage = create_storage(config)
    conn = storage.conn
    ph = "%s" if config.storage.backend == "postgres" else "?"
    since = time.time() - args.since_days * 86400 if args.since_days else 0

    rows = conn.execute(SELECT.format(ph=ph), (since,)).fetchall()
    updates: list[tuple[float, int]] = []
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    unpriced: dict[str, int] = defaultdict(int)
    for row in rows:
        rid, model, t_in, t_out, c_read, c_write = tuple(row)
        price = pricing.lookup_price(model)
        if price is None:
            unpriced[model or "(none)"] += 1
            continue
        cost = pricing.estimate_cost(price, t_in or 0, t_out or 0, c_read, c_write, 0)
        if cost <= 0:
            continue
        updates.append((cost, rid))
        totals[model][0] += 1
        totals[model][1] += cost

    print(f"{'model':<32} {'rows':>8} {'cost USD':>12}")
    for model, (n, cost) in sorted(totals.items(), key=lambda kv: -kv[1][1]):
        print(f"{model:<32} {int(n):>8} {cost:>12.2f}")
    print(f"{'TOTAL':<32} {len(updates):>8} {sum(c for c, _ in updates):>12.2f}")
    if unpriced:
        print("\nStill unpriced (left at 0):")
        for model, n in sorted(unpriced.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {model}: {n}")

    if not args.apply:
        print("\nDry run - nothing written. Re-run with --apply to update.")
        return 0

    sql = UPDATE.format(ph=ph)
    for i in range(0, len(updates), 1000):
        batch = updates[i : i + 1000]
        if hasattr(conn, "executemany"):
            conn.executemany(sql, batch)
        else:
            cur = conn.cursor()
            cur.executemany(sql, batch)
        conn.commit()
    print(f"\nUpdated {len(updates)} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
