# AI Portfolio Bot, nach Lopez-Lira-Methodik

Zwei Stufen:
- `ai_portfolio.py` thinking: Makro → S&P-500-Scoring → 15-Positionen-Zielportfolio.
- `etoro_executor.py` trading: gleicht das Ziel mit eToro-Agent-Portfolio ab und rebalanciert



## One-time-Setup
1. eToro öffnen → Agent Portfolios (Beta) im Seitenmenü → Portfolio anlegen,
   Namen + Investitionsbetrag wählen (min. ~$200) und **finanzieren**.
2. Den einmalig angezeigten **user token** sicher speichern.
3. Anthropic-API-Key besorgen (muss aufgeladen werden).

## Installation
```bash
pip install anthropic yfinance pandas lxml requests
export ANTHROPIC_API_KEY=sk-ant-...
export ETORO_USER_KEY=<dein agent-portfolio user token>
```

## Monthly runf
```bash
# 1) Zielportfolio erzeugen
python ai_portfolio.py --out target_portfolio.json

# 2) Erst ansehen, was gehandelt würde (Default: Dry-Run, sendet nichts)
python etoro_executor.py --target target_portfolio.json

# 3) Wenn der Plan passt: live setzen und execute
ETORO_LIVE=1 python etoro_executor.py --target target_portfolio.json
```
Als Cron (z. B. am 1. des Monats): beide Schritte hintereinander; Schritt 3 nur
mit gesetztem ETORO_LIVE=1.

## Sicherheitsnetze (in etoro_executor.py)
- Dry-Run ist Default; live nur mit ETORO_LIVE=1.
- Gewichtssumme ~100 %, Cap pro Position (MAX_WEIGHT), Mindest-Order (MIN_ORDER_USD),
  Rebalancing-Schwelle gegen Mikro-Trades.
- Rate-Limit-Spacing + 429-Backoff; "erst schließen, 60 s warten, dann öffnen".
- User token nur aus ENV, nie im Code.

## Offline testen
```bash
python ai_portfolio.py --mock
python etoro_executor.py --mock --target demo.json
```

## Noch zu verifizieren / verfeinern
- Feldnamen der Live-PnL-Antwort (units / positionID) gegen die echte API prüfen —
  partielle Closes nutzen `units`; fehlen sie, besser Full-Close + Neueröffnung.
- Optionaler News-Provider in `get_news()` (stocknewsapi o. Ä.) für höhere Treue.
