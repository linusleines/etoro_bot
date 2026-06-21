"""
Stop-loss watcher for the eToro agent portfolio.

Run this on a schedule (e.g. daily). It checks every open position's loss
versus its entry and closes any that breach the threshold.

  * Per-position fixed stop: close if unrealized loss <= -STOP_LOSS_PCT.
  * Dry-run by default; closes only when ETORO_LIVE=1.
  * Reuses the executor's validated API calls (key check, pnl, close).

Usage:
    python stop_loss.py                 # dry-run: shows what would close
    $env:ETORO_LIVE="1"; python stop_loss.py   # enforce (PowerShell)
"""

import os, sys, time
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import etoro_executor as ex

# Close a position once it is this far below its entry (0.15 = -15%).
STOP_LOSS_PCT = 0.25

def main():
    if not ex.USER_KEY:
        sys.exit("Set ETORO_USER_KEY first.")
    ex.assert_agent_portfolio_key()
    live = os.environ.get("ETORO_LIVE") == "1"

    cp = ex.get_pnl()
    breached = []
    for p in cp.get("positions", []):
        amount = p.get("amount") or 0
        pnl = (p.get("unrealizedPnL") or {}).get("pnL") or 0
        if amount <= 0:
            continue
        loss_pct = pnl / amount
        if loss_pct <= -STOP_LOSS_PCT:
            breached.append((p, loss_pct))

    if not breached:
        print(f"No position past -{STOP_LOSS_PCT*100:.0f}%. Nothing to do.")
        return

    print(f"{'LIVE' if live else 'DRY-RUN'} — stop-loss breaches "
          f"(threshold -{STOP_LOSS_PCT*100:.0f}%):")
    for p, lp in breached:
        print(f"  instrument={p.get('instrumentID')} position={p.get('positionID')} "
              f"loss={lp*100:6.1f}%")

    if not live:
        print("Dry-run: nothing closed. Set ETORO_LIVE=1 to enforce.")
        return

    for p, _ in breached:
        ex.close_position(p.get("positionID"), p.get("instrumentID"), None)
        time.sleep(ex.REQUEST_SPACING)
    print(f"Closed {len(breached)} position(s) via stop-loss.")

if __name__ == "__main__":
    main()