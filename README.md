# AI Portfolio Bot

A self-hosted bot that rebuilds the Lopez-Lira "AI portfolio" methodology with
your own LLM: it generates a monthly target portfolio and rebalances it inside
an eToro Agent Portfolio. The output is your own run of the recipe — it will not
match anyone else's portfolio exactly.

## How it works

Two stages:

- **`ai_portfolio.py` (thinking)** — builds a live macro report (via web search),
  scores every S&P 500 stock plus a set of ETFs, picks the top 10 stocks and top
  5 ETFs, and allocates a 15-position weighted target → `target_portfolio.json`.
- **`etoro_executor.py` (trading)** — reads that target, compares it to your live
  agent-portfolio holdings, and rebalances: close/reduce first, then open.

**Models:** Claude by default — Haiku for the ~520 scoring calls, Opus for the
macro report and the allocation. Swappable to DeepSeek via `Settings.provider`.

**Cost levers (Anthropic):** the scoring calls run as one Batch (−50%) with the
shared macro/instructions block prompt-cached (−90% on the cached input). A
`run_cache.json` stores the day's macro + scores, so a same-day re-run only
redoes the cheap allocation; `--fresh` forces a full rescore. A run costs
roughly $1–2.

## One-time setup

1. In eToro: **Agent Portfolios (Beta)** → create a portfolio, name it, and
   **fund it** (min. ~$200). This isolated portfolio is the only thing the bot
   trades — your main account is never touched.
2. Save the **user token** shown once at creation.
3. Get an **Anthropic API key** (pay-as-you-go; needs credit).

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root (never commit it — it is gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
ETORO_USER_KEY=<agent-portfolio user token>
```

## Monthly run

```bash
python ai_portfolio.py                  # generate target_portfolio.json (live macro)
python etoro_executor.py                # dry-run: prints the planned trades, sends nothing
ETORO_LIVE=1 python etoro_executor.py   # execute the rebalance live
```

The executor accounts for your existing holdings automatically: it trades only
the difference between your current positions and the new target.

## Stop-loss (run daily)

```bash
python stop_loss.py                     # dry-run: shows what would close
ETORO_LIVE=1 python stop_loss.py        # enforce: closes positions past the threshold
```

Threshold is `STOP_LOSS_PCT` in `stop_loss.py` (default −15% per position).

## Automation

- **macOS (launchd):** `run_monthly.sh` and `run_stoploss.sh`, scheduled by the
  `com.linus.etoro.*.plist` agents in `~/Library/LaunchAgents` — monthly
  rebalance on the 1st, daily stop-loss.
- **Windows (Task Scheduler):** `run_monthly.bat` and `run_stoploss.bat`,
  registered with `schtasks`.

## Safety

- Dry-run by default; live trades only when `ETORO_LIVE=1`.
- Aborts if the key controls a **main account** instead of the isolated agent
  portfolio — it will never rebalance your whole real account.
- The target is validated before any trade: weights sum to ~100%, per-position
  cap (`MAX_WEIGHT`), max positions, minimum order size (`MIN_ORDER_USD`), and a
  rebalance threshold that skips micro-trades. Duplicate and placeholder
  positions are removed and the weights renormalized.
- Rebalance order: close/reduce first, wait 60s for the balance to settle, then
  open — it never buys with cash it has not freed yet.
- Rate-limit spacing plus 429 back-off.
- Never creates accounts, deposits, or withdraws — it only trades within the
  existing agent portfolio. Tokens are read from `.env` only, never hard-coded.

## Offline test (no keys, no trades)

```bash
python ai_portfolio.py --mock
python etoro_executor.py --mock --target demo.json
```

## Configuration

Edit `Settings` in `ai_portfolio.py`:

- `provider`, `scoring_model`, `reasoning_model` — the LLM setup.
- `use_batch`, `use_cache`, `macro_web_search` — cost levers and live-data macro.
- `top_stocks`, `top_etfs`, `target_positions` — portfolio shape.
- Token limits and thresholds.
- Per-firm news is off by default; plug a provider into `get_news()` and set
  `use_news = True` to enable it for higher fidelity (needs a paid news API).

## Note

Not financial advice. You own the strategy and its risk. The edge of AI-driven
portfolios is historically inconsistent, and this is your own DIY run of the
method.
