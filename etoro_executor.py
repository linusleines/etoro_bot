"""
eToro Agent-Portfolio executor (Stage 2).

Reads target_portfolio.json (from ai_portfolio.py), compares it to the live
agent-portfolio, and rebalances via the eToro Agent API.

SAFETY:
  * Dry-run by default. Live trading only when ETORO_LIVE=1 is set.
  * Guardrails run before any live order (weight sum, per-position cap,
    min order size, rebalance threshold).
  * The agent-portfolio must already exist and be funded BY YOU. This bot
    never creates accounts, deposits, or withdraws funds — it only trades
    within an existing agent-portfolio.
  * The user token is read from ETORO_USER_KEY (never hardcoded).

Offline demo (no network, no keys):
    python etoro_executor.py --mock --target demo.json
"""

from __future__ import annotations
import argparse, json, os, sys, time, uuid
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

BASE = "https://public-api.etoro.com/api/v1"
# Public shared key from eToro's own agent skill; override via env if needed.
X_API_KEY = os.environ.get("ETORO_API_KEY",
    "sdgdskldFPLGfjHn1421dgnlxdGTbngdflg6290bRjslfihsjhSDsdgGHH25hjf")
USER_KEY = os.environ.get("ETORO_USER_KEY", "")
LIVE = os.environ.get("ETORO_LIVE") == "1"

# Guardrails
MAX_WEIGHT = 0.40          # reject target if any single weight exceeds this
MAX_POSITIONS = 20
REBALANCE_THRESHOLD = 0.005  # skip drifts smaller than 0.5% of equity
MIN_ORDER_USD = 10.0
REQUEST_SPACING = 3.1      # seconds between trade calls (limit: 20/min)
PNL_CACHE_WAIT = 60        # PnL endpoint caches for 60s after a close

_MOCK = False

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _headers():
    return {"x-api-key": X_API_KEY, "x-user-key": USER_KEY,
            "x-request-id": str(uuid.uuid4()), "Content-Type": "application/json"}

def _request(method: str, path: str, body: dict | None = None, _retries=0):
    if _MOCK:
        return _mock_api(method, path, body)
    import requests
    url = path if path.startswith("http") else f"{BASE}{path}"
    r = requests.request(method, url, headers=_headers(), json=body, timeout=30)
    if r.status_code == 429:
        wait = min(15 * (2 ** _retries), 60)
        print(f"  rate limited, waiting {wait}s")
        time.sleep(wait)
        return _request(method, path, body, _retries + 1)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.text else {}

# --------------------------------------------------------------------------- #
# Safety: make sure we are NOT pointed at the whole main account
# --------------------------------------------------------------------------- #

def assert_agent_portfolio_key():
    """Agent-portfolio key -> GET /agent-portfolios returns 403 (good).
    Main-account key -> 200, meaning the bot would trade the ENTIRE real
    account. Abort hard in that case."""
    if _MOCK:
        return
    import requests
    r = requests.get(f"{BASE}/agent-portfolios", headers=_headers(), timeout=30)
    if r.status_code == 200:
        sys.exit("ABORT: this is a MAIN-ACCOUNT key. The bot would rebalance "
                 "your whole real account. Create an Agent Portfolio and use "
                 "its token as ETORO_USER_KEY instead.")
    if r.status_code != 403:
        print(f"warning: unexpected key-check status {r.status_code}: {r.text[:200]}")

# --------------------------------------------------------------------------- #
# Portfolio reads
# --------------------------------------------------------------------------- #

def get_pnl() -> dict:
    return _request("GET", "/trading/info/real/pnl")["clientPortfolio"]

def equity_and_cash(cp: dict) -> tuple[float, float]:
    """Faithful to the skill's formulas (manual positions; mirrors handled if present)."""
    positions = cp.get("positions", [])
    orders = cp.get("orders", [])
    opens = [o for o in cp.get("ordersForOpen", []) if o.get("mirrorID", 0) == 0]
    invested = sum(p["amount"] for p in positions) \
        + sum(o["amount"] for o in opens) + sum(o["amount"] for o in orders)
    upnl = sum(p.get("unrealizedPnL", {}).get("pnL", 0) for p in positions)
    cash = cp.get("credit", 0) - sum(o["amount"] for o in opens) \
        - sum(o["amount"] for o in orders)
    equity = cash + invested + upnl
    return equity, cash

def current_positions(cp: dict) -> dict[int, dict]:
    """Keyed by instrumentID -> {amount, positionID, units}."""
    out = {}
    for p in cp.get("positions", []):
        iid = p.get("instrumentID")
        out[iid] = {"amount": p["amount"], "positionID": p.get("positionID"),
                    "units": p.get("units")}
    return out

# --------------------------------------------------------------------------- #
# Instruments / orders
# --------------------------------------------------------------------------- #

_id_cache: dict[str, int] = {}
def resolve_instrument_id(symbol: str) -> int:
    if symbol in _id_cache:
        return _id_cache[symbol]
    res = _request("GET", f"/market-data/search?internalSymbolFull={symbol}")
    for item in res.get("items", []):
        if item.get("internalSymbolFull") == symbol:
            _id_cache[symbol] = item["instrumentId"]   # lowercase d here
            return _id_cache[symbol]
    raise ValueError(f"instrument not found: {symbol}")

def open_by_amount(instrument_id: int, amount: float):
    return _request("POST", "/trading/execution/market-open-orders/by-amount",
                    {"InstrumentID": instrument_id, "IsBuy": True,
                     "Leverage": 1, "Amount": round(amount, 2)})

def close_position(position_id, instrument_id: int, units_to_deduct=None):
    return _request("POST",
        f"/trading/execution/market-close-orders/positions/{position_id}",
        {"InstrumentId": instrument_id, "UnitsToDeduct": units_to_deduct})

# --------------------------------------------------------------------------- #
# Rebalancing
# --------------------------------------------------------------------------- #

def validate_target(positions: list[dict]):
    if not positions:
        raise ValueError("target portfolio is empty")
    if len(positions) > MAX_POSITIONS:
        raise ValueError(f"too many positions ({len(positions)} > {MAX_POSITIONS})")
    total = sum(p["weight"] for p in positions)
    if abs(total - 1.0) > 0.05:
        raise ValueError(f"weights sum to {total:.3f}, expected ~1.0")
    for p in positions:
        if p["weight"] < 0 or p["weight"] > MAX_WEIGHT:
            raise ValueError(f"weight {p['weight']} for {p['ticker']} fails cap {MAX_WEIGHT}")

def plan_rebalance(target: list[dict], cp: dict) -> list[dict]:
    equity, _ = equity_and_cash(cp)
    if equity <= 0:
        raise RuntimeError("equity <= 0, refusing to trade")
    current = current_positions(cp)
    # resolve target symbols to instrument ids
    target_by_id = {}
    for p in target:
        iid = resolve_instrument_id(p["ticker"])
        target_by_id[iid] = {"ticker": p["ticker"], "amount": p["weight"] * equity}

    plan = []
    threshold = REBALANCE_THRESHOLD * equity
    for iid, cur in current.items():                       # reduce / close
        tgt = target_by_id.get(iid, {}).get("amount", 0.0)
        delta = tgt - cur["amount"]
        if tgt == 0:
            plan.append({"action": "close", "iid": iid, "positionID": cur["positionID"],
                         "ticker": _symbol_of(iid), "amount": cur["amount"]})
        elif delta < -threshold:
            frac = min(1.0, (cur["amount"] - tgt) / cur["amount"])
            units = round(cur["units"] * frac, 6) if cur.get("units") else None
            plan.append({"action": "reduce", "iid": iid, "positionID": cur["positionID"],
                         "ticker": _symbol_of(iid), "amount": cur["amount"] - tgt,
                         "units": units})
    for iid, tgt in target_by_id.items():                  # open / increase
        cur_amt = current.get(iid, {}).get("amount", 0.0)
        delta = tgt["amount"] - cur_amt
        if delta > threshold and delta >= MIN_ORDER_USD:
            plan.append({"action": "open", "iid": iid, "ticker": tgt["ticker"],
                         "amount": delta})
    return plan

def _symbol_of(iid):
    for sym, cached in _id_cache.items():
        if cached == iid:
            return sym
    return str(iid)

def execute_plan(plan: list[dict], equity: float):
    closes = [a for a in plan if a["action"] in ("close", "reduce")]
    opens = [a for a in plan if a["action"] == "open"]

    print(f"\nPlanned rebalance ({'LIVE' if LIVE else 'DRY-RUN'}):")
    for a in closes + opens:
        pct = a["amount"] / equity * 100
        print(f"  {a['action']:<7} {a['ticker']:<8} {pct:5.2f}% of equity")
    if not LIVE:
        print("\nDry-run: no orders sent. Set ETORO_LIVE=1 to execute.")
        return

    for a in closes:                                       # phase 1: free cash
        close_position(a["positionID"], a["iid"], a.get("units"))
        time.sleep(REQUEST_SPACING)
    if closes:
        print(f"  waiting {PNL_CACHE_WAIT}s for PnL cache to refresh")
        time.sleep(PNL_CACHE_WAIT)
        equity, cash = equity_and_cash(get_pnl())          # verify
        print(f"  available cash now ~{cash/equity*100:.1f}% of equity")
    for a in opens:                                        # phase 2: deploy
        open_by_amount(a["iid"], a["amount"])
        time.sleep(REQUEST_SPACING)
    print("Rebalance submitted.")

# --------------------------------------------------------------------------- #
# Mock API (offline demo of the diff + dry-run plan)
# --------------------------------------------------------------------------- #

def _mock_api(method, path, body):
    if path.endswith("/pnl") or "/info/real/pnl" in path:
        return {"clientPortfolio": {
            "credit": 7500,
            "positions": [
                {"instrumentID": 1001, "amount": 2000, "positionID": "p-aapl",
                 "units": 10, "unrealizedPnL": {"pnL": 0}},   # AAPL: not in target
                {"instrumentID": 1002, "amount": 500, "positionID": "p-jpm",
                 "units": 3, "unrealizedPnL": {"pnL": 0}},     # JPM: below target
            ], "mirrors": [], "orders": [], "ordersForOpen": []}}
    if "/market-data/search" in path:
        sym = path.split("internalSymbolFull=")[1]
        fake = {"AAPL": 1001, "JPM": 1002}
        return {"items": [{"internalSymbolFull": sym,
                           "instrumentId": fake.get(sym, abs(hash(sym)) % 9000 + 2000)}]}
    return {"ok": True}

# --------------------------------------------------------------------------- #

def main():
    global _MOCK, LIVE
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="target_portfolio.json")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    _MOCK = args.mock
    if _MOCK:
        LIVE = False

    if not _MOCK and not USER_KEY:
        sys.exit("Set ETORO_USER_KEY (agent-portfolio user token) first.")
    assert_agent_portfolio_key()

    target = json.load(open(args.target))["positions"]
    validate_target(target)
    cp = get_pnl()
    equity, cash = equity_and_cash(cp)
    plan = plan_rebalance(target, cp)
    if not plan:
        print("Already balanced within threshold. Nothing to do.")
        return
    execute_plan(plan, equity)

if __name__ == "__main__":
    main()