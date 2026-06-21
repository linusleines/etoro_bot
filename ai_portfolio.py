"""
AI Portfolio Engine — rebuilds the Lopez-Lira methodology (white paper) with a
swappable LLM brain (Claude by default, DeepSeek R1 optional).

Stages:
  1. macro report        generate_macro()            [reasoning model]
  2. firm scoring        batch_score_all / score_sync [cheap model, batched+cached]
  3. selection           select_top()
  4. allocation          allocate_portfolio()        [reasoning model]

Cost levers (Anthropic): Batch API (-50%) + prompt caching of the shared system
block (-90% cached). A run_cache.json stores the macro + scores for the day so a
re-run (e.g. after an allocation tweak) does NOT re-pay for the ~520 scorings.
Use --fresh to force a full rescore.

Offline demo:  python ai_portfolio.py --mock
"""

from __future__ import annotations
import argparse, json, os, re, sys, hashlib, time, datetime as dt
from dataclasses import dataclass, asdict
from typing import Optional
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

CACHE_FILE = "run_cache.json"

@dataclass
class Settings:
    provider: str = "anthropic"          # "anthropic" | "deepseek"
    scoring_model: str = "claude-haiku-4-5-20251001"   # DeepSeek: "deepseek-chat"
    reasoning_model: str = "claude-opus-4-8"           # DeepSeek: "deepseek-reasoner"
    top_stocks: int = 10
    top_etfs: int = 5
    target_positions: int = 15
    max_workers: int = 8
    use_news: bool = False
    use_batch: bool = True
    use_cache: bool = True
    batch_poll_seconds: int = 15
    score_max_tokens: int = 2000
    macro_max_tokens: int = 3000
    alloc_max_tokens: int = 8000
    macro_web_search: bool = True
    macro_search_max_uses: int = 5

SETTINGS = Settings()

ETF_UNIVERSE = ["SPY", "QQQ", "XLP", "XLV", "XLE", "XLF", "XLK", "XLI", "XLU",
                "XLY", "XLB", "XLRE", "PBW", "PHO", "IHE", "XHB", "TIP", "IEF",
                "TLT", "SHY", "GLD", "VNQ"]
ETF_SET = set(ETF_UNIVERSE)


FIRM_SYSTEM = (
    "You are a financial expert with stock recommendation experience. "
    "Speak in the third person. You do not mention your credentials. "
    "You provide investment scores (1 to 100) for the next month for companies "
    "based on news and financial data.\n"
    "Here is some macro-economic data for context.\n{macro}\n"
    "For the company given by the user, first write a short investment report "
    "about the firm situation, with sections on the recent news, financials, "
    "valuations, and economic outlook affecting the firm. Interpret the news "
    "rather than just repeating it. Do not recommend alternatives. Do not speak "
    "directly to investors nor recommend actions.\n"
    "Start with 'Investment Report:'. Finally, on a new line, output Score: X."
)

FIRM_USER = (
    "Assess company {name} in the {industry} industry for the next month.\n"
    "Recent financial data:\n{financials}\n\nRecent news:\n{news}\n"
)

MACRO_PROMPT = (
    "You are a macro strategist. Today is {today}. Search the web for the most "
    "recent US economic data, Federal Reserve communications, inflation and "
    "labor prints, tariff/trade developments, and major market-moving events. "
    "Base your analysis on what you find, not on priors.\n"
    "Provide a concise expected timeline of the most important economic and "
    "political events for the next three months in the USA, with special "
    "attention to the next month. Include your own expectations, not only "
    "consensus. End with a short forecast table for interest rates, inflation, "
    "and tariffs for next month and quarter.\n\nAdditional context:\n{context}\n"
)

ALLOC_PROMPT = (
    "{macro}\n\n"
    "Build a {n}-asset portfolio to hold for the next month (rebalanced in one "
    "month), weighted to perform positively given market conditions and to beat "
    "the S&P 500. You have the following reports for the highest-scored "
    "stocks:\n{reports}\n\n"
    "You may also use most ETFs (NOT short, leveraged, or volatility products), "
    "including market, sector, TIPS, and long/short-term bond ETFs. You decide "
    "the weights; you need not include any or all of the instruments mentioned.\n"
    "Return ONLY valid JSON, no prose, no markdown fences, with this schema:\n"
    '{{"positions": [{{"ticker": "STR", "instrument_type": "stock|etf", '
    '"weight": 0.0, "thesis": "STR", "edge": "STR", "risk": "STR"}}]}}\n'
    "Provide up to {n} DISTINCT positions (fewer is fine). Never repeat a "
    "ticker and never output placeholder entries. Weights are decimals that "
    "sum to 1.0. Keep thesis, edge, and risk to one short sentence each.\n"
    "Candidate ETFs with scores:\n{etfs}\n"
)

def _strip_reasoning(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

def complete(prompt: str, model: str, settings: Settings = SETTINGS,
             max_tokens: int = 2000, web_search: bool = False) -> str:
    if settings.provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        kwargs = {"model": model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}
        if web_search:
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search",
                                "max_uses": settings.macro_search_max_uses}]
        msg = client.messages.create(**kwargs)
        return _strip_reasoning("".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"))
    elif settings.provider == "deepseek":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                        base_url="https://api.deepseek.com")
        r = client.chat.completions.create(model=model,
            messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens)
        return _strip_reasoning(r.choices[0].message.content)
    raise ValueError(f"unknown provider {settings.provider}")

def complete_cached(system: str, user: str, model: str,
                    settings: Settings = SETTINGS, max_tokens: int = 2000) -> str:
    if settings.provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()
        block = {"type": "text", "text": system}
        if settings.use_cache:
            block["cache_control"] = {"type": "ephemeral"}
        msg = client.messages.create(model=model, max_tokens=max_tokens,
            system=[block], messages=[{"role": "user", "content": user}])
        return _strip_reasoning("".join(b.text for b in msg.content if b.type == "text"))
    return complete(system + "\n\n" + user, model, settings, max_tokens)


_MOCK = False
def _mock_complete(prompt: str) -> str:
    if "Build a" in prompt and "JSON" in prompt:
        tickers = re.findall(r"### ([A-Z\.]{1,6}) \(", prompt)[:SETTINGS.target_positions]
        if len(tickers) < SETTINGS.target_positions:
            tickers += ["TIP", "IEF", "GLD", "XLP", "SPY"][: SETTINGS.target_positions - len(tickers)]
        tickers = tickers[:SETTINGS.target_positions]
        w = round(1 / len(tickers), 4)
        pos = [{"ticker": t, "instrument_type": "etf" if t in ETF_SET else "stock",
                "weight": w, "thesis": "mock", "edge": "mock", "risk": "mock"} for t in tickers]
        return json.dumps({"positions": pos})
    if "macro strategist" in prompt:
        return "Mock macro outlook: rates steady, inflation easing, tariffs flat."
    name = re.search(r"company (.+?) in the", prompt)
    seed = int(hashlib.md5((name.group(1) if name else prompt).encode()).hexdigest(), 16)
    return f"Investment Report:\nMock report.\nScore: {40 + seed % 56}"

def _llm(prompt: str, model: str, max_tokens: int = 2000) -> str:
    return _mock_complete(prompt) if _MOCK else complete(prompt, model, max_tokens=max_tokens)


def get_sp500_universe() -> list[dict]:
    if _MOCK:
        return [{"ticker": t, "name": t, "industry": "Mock"} for t in
                ["AAPL", "MSFT", "NVDA", "META", "JPM", "V", "UNH", "HCA",
                 "NFLX", "NOW", "LLY", "GOOG", "UNP", "CRM", "TMO"]]
    import pandas as pd
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                      storage_options={"User-Agent": ua})[0]
    return [{"ticker": str(r["Symbol"]).replace(".", "-"),
             "name": r["Security"],
             "industry": r.get("GICS Sub-Industry", "")}
            for r in df.to_dict("records")]

def get_financials(ticker: str) -> dict:
    if _MOCK:
        return {"trailingPE": 25, "marketCap": 1e11, "profitMargins": 0.2}
    import yfinance as yf
    return yf.Ticker(ticker).info

def safe_financials(ticker: str) -> Optional[dict]:
    try:
        fin = get_financials(ticker)
        return fin if fin else None
    except Exception as e:
        print(f"  skip {ticker}: data fetch failed ({type(e).__name__})")
        return None

def get_news(ticker: str) -> str:
    return "" if not SETTINGS.use_news else "(news provider not configured)"

def get_market_cap(fin: dict) -> float:
    return float(fin.get("marketCap") or 0)

@dataclass
class Scored:
    ticker: str
    name: str
    industry: str
    score: int
    report: str
    market_cap: float = 0.0

def generate_macro(context: str) -> str:
    prompt = MACRO_PROMPT.format(context=context, today=dt.date.today().isoformat())
    if _MOCK:
        return _mock_complete(prompt)
    return complete(prompt, SETTINGS.reasoning_model,
                    max_tokens=SETTINGS.macro_max_tokens,
                    web_search=SETTINGS.macro_web_search)

def _parse_score(text: str) -> Optional[int]:
    m = re.search(r"Score:\s*(\d{1,3})", text)
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 100 else None

def _user_prompt(firm: dict, fin: dict) -> str:
    return FIRM_USER.format(name=firm["name"], industry=firm["industry"],
                            financials=json.dumps(fin)[:4000],
                            news=get_news(firm["ticker"]) or "(none)")

def score_one(firm: dict, system: str) -> Optional[Scored]:
    fin = safe_financials(firm["ticker"])
    if fin is None:
        return None
    user = _user_prompt(firm, fin)
    out = _mock_complete(user) if _MOCK else \
        complete_cached(system, user, SETTINGS.scoring_model,
                        max_tokens=SETTINGS.score_max_tokens)
    s = _parse_score(out)
    if s is None:
        return None
    return Scored(firm["ticker"], firm["name"], firm["industry"], s, out,
                  get_market_cap(fin))

def score_sync(firms: list[dict], macro: str) -> list[Scored]:
    from concurrent.futures import ThreadPoolExecutor
    system = FIRM_SYSTEM.format(macro=macro)
    workers = 1 if _MOCK else SETTINGS.max_workers
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda f: score_one(f, system), firms))
    return [r for r in results if r]

def batch_score_all(firms: list[dict], macro: str) -> list[Scored]:
    import anthropic
    client = anthropic.Anthropic()
    system = FIRM_SYSTEM.format(macro=macro)
    sys_block = {"type": "text", "text": system}
    if SETTINGS.use_cache:
        sys_block["cache_control"] = {"type": "ephemeral"}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=SETTINGS.max_workers) as ex:
        raw = list(ex.map(lambda f: (f, safe_financials(f["ticker"])), firms))
    prepared = [(f, fin) for f, fin in raw if fin is not None]
    print(f"  fetched data for {len(prepared)}/{len(firms)} firms")
    meta = {f["ticker"]: (f, fin) for f, fin in prepared}

    requests = [{
        "custom_id": f["ticker"],
        "params": {"model": SETTINGS.scoring_model,
                   "max_tokens": SETTINGS.score_max_tokens,
                   "system": [sys_block],
                   "messages": [{"role": "user", "content": _user_prompt(f, fin)}]},
    } for f, fin in prepared]

    batch = client.messages.batches.create(requests=requests)
    print(f"  submitted batch {batch.id} with {len(requests)} requests")
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        print(f"  batch status={b.processing_status} counts={b.request_counts}")
        time.sleep(SETTINGS.batch_poll_seconds)

    scored = []
    for entry in client.messages.batches.results(batch.id):
        if entry.result.type != "succeeded":
            continue
        firm, fin = meta[entry.custom_id]
        text = _strip_reasoning("".join(
            blk.text for blk in entry.result.message.content if blk.type == "text"))
        s = _parse_score(text)
        if s is not None:
            scored.append(Scored(firm["ticker"], firm["name"], firm["industry"],
                                 s, text, get_market_cap(fin)))
    return scored

def select_top(scored: list[Scored], n: int) -> list[Scored]:
    return sorted(scored, key=lambda s: (-s.score, -s.market_cap))[:n]

def allocate_portfolio(top_stocks: list[Scored], top_etfs: list[Scored],
                       macro: str) -> list[dict]:
    reports = "\n\n".join(f"### {s.ticker} ({s.name}, score {s.score})\n{s.report}"
                          for s in top_stocks)
    etf_lines = "\n".join(f"{e.ticker}: score {e.score}" for e in top_etfs)
    prompt = ALLOC_PROMPT.format(macro=macro, n=SETTINGS.target_positions,
                                 reports=reports, etfs=etf_lines)
    raw = _llm(prompt, SETTINGS.reasoning_model, SETTINGS.alloc_max_tokens).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        positions = json.loads(raw)["positions"]
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"allocation JSON did not parse ({e}). Output length {len(raw)} — "
            f"likely truncated; raise Settings.alloc_max_tokens. "
            f"Tail: ...{raw[-150:]}")
    seen: dict[str, dict] = {}
    for p in positions:
        if float(p.get("weight", 0)) <= 0:
            continue
        if str(p.get("thesis", "")).strip().lower() == "placeholder":
            continue
        t = p["ticker"]
        if t not in seen or p["weight"] > seen[t]["weight"]:
            seen[t] = p
    positions = list(seen.values())
    if not positions:
        raise RuntimeError("allocation produced no valid positions")
    total = sum(p["weight"] for p in positions) or 1.0
    for p in positions:
        p["weight"] = round(p["weight"] / total, 4)
    return positions

def run_monthly(context: str = "", fresh: bool = False) -> dict:
    macro, scored = None, None
    if not _MOCK and not fresh and os.path.exists(CACHE_FILE):
        try:
            data = json.load(open(CACHE_FILE))
            if data.get("date") == dt.date.today().isoformat():
                macro = data["macro"]
                scored = [Scored(**s) for s in data["scored"]]
                print(f"Using cached macro + {len(scored)} scores from today "
                      f"(run with --fresh to rescore).")
        except Exception:
            macro, scored = None, None

    if scored is None:
        print("[1/4] generating macro report ...")
        macro = generate_macro(context or "No external context supplied.")
        firms = get_sp500_universe() + [
            {"ticker": t, "name": t, "industry": "ETF"} for t in ETF_UNIVERSE]
        use_batch = (not _MOCK and SETTINGS.use_batch and SETTINGS.provider == "anthropic")
        print(f"[2/4] scoring {len(firms)} firms ({'batch' if use_batch else 'sync'}) "
              f"— this is the slow part, please wait ...")
        scored = batch_score_all(firms, macro) if use_batch else score_sync(firms, macro)
        print(f"      {len(scored)} firms scored")
        if not _MOCK:
            json.dump({"date": dt.date.today().isoformat(), "macro": macro,
                       "scored": [asdict(s) for s in scored]}, open(CACHE_FILE, "w"))

    stocks = [s for s in scored if s.ticker not in ETF_SET]
    etfs = [s for s in scored if s.ticker in ETF_SET]
    top_stocks = select_top(stocks, SETTINGS.top_stocks)
    top_etfs = select_top(etfs, SETTINGS.top_etfs)
    print("[3/4] allocating 15-position portfolio ...")
    positions = allocate_portfolio(top_stocks, top_etfs, macro)
    print("[4/4] done")
    return {"date": dt.date.today().isoformat(),
            "model": f"{SETTINGS.provider} scoring={SETTINGS.scoring_model} "
                     f"reasoning={SETTINGS.reasoning_model}",
            "macro": macro, "positions": positions}


def main():
    global _MOCK
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="run offline with fake data/LLM")
    ap.add_argument("--fresh", action="store_true", help="ignore run_cache.json and rescore")
    ap.add_argument("--out", default="target_portfolio.json")
    args = ap.parse_args()
    _MOCK = args.mock
    portfolio = run_monthly(fresh=args.fresh)
    with open(args.out, "w") as f:
        json.dump(portfolio, f, indent=2)
    print(f"{portfolio['date']}  {portfolio['model']}")
    print(f"{'TICKER':<8}{'TYPE':<7}{'WEIGHT':>8}")
    for p in portfolio["positions"]:
        print(f"{p['ticker']:<8}{p['instrument_type']:<7}{p['weight']*100:>7.2f}%")
    print(f"\nSaved -> {args.out}")

if __name__ == "__main__":
    main()