"""Live smoke test against the real APIs — run from a machine with open egress.

  python -m app.smoke --polymarket 0xYourWalletAddress
  python -m app.smoke --kalshi-key-id <key-id> --kalshi-key-file private_key.pem

Day-1 validation: run --polymarket for each participant's address. If a
participant who trades on the Polymarket US iOS app comes back with $0 and no
fills despite having positions, their account isn't covered by the public data
API — have them use Kalshi for the competition instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--polymarket", metavar="WALLET", help="Polymarket wallet address")
    parser.add_argument("--kalshi-key-id", metavar="KEY_ID")
    parser.add_argument("--kalshi-key-file", metavar="PEM_PATH")
    args = parser.parse_args()

    if not args.polymarket and not args.kalshi_key_id:
        parser.print_help()
        return 2

    if args.polymarket:
        from app.connectors.polymarket import PolymarketConnector

        c = PolymarketConnector(args.polymarket)
        state = await c.fetch_state()
        fills = await c.fetch_fills()
        print(f"[polymarket] cash=${state.cash:.2f} positions=${state.positions_value:.2f} "
              f"total=${state.total:.2f}")
        for f in fills[:5]:
            print(f"  {f.ts:%m-%d %H:%M} {f.side} {f.size:.0f}x {f.outcome} "
                  f"@ {f.price * 100:.0f}c — {f.market_title}")
        if state.total == 0 and not fills:
            print("  WARNING: empty account — wrong address, or a US-app account "
                  "not covered by the public data API")

    if args.kalshi_key_id:
        if not args.kalshi_key_file:
            print("--kalshi-key-file is required with --kalshi-key-id", file=sys.stderr)
            return 2
        from app.connectors.kalshi import KalshiConnector

        with open(args.kalshi_key_file) as fh:
            pem = fh.read()
        c = KalshiConnector(args.kalshi_key_id, pem)
        state = await c.fetch_state()
        fills = await c.fetch_fills()
        print(f"[kalshi] cash=${state.cash:.2f} positions=${state.positions_value:.2f} "
              f"total=${state.total:.2f}")
        for f in fills[:5]:
            print(f"  {f.ts:%m-%d %H:%M} {f.side} {f.size:.0f}x {f.outcome} "
                  f"@ {f.price * 100:.0f}c — {f.market_title}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
