# Legend Scalper — "always trades" preset

A deliberately loose preset. Every entry gate is pushed to its floor so the desk
always has a candidate to trade. It is the opposite of selective: expect a low
win rate and many small losses. Use it to keep the desk busy / generating data,
not as a profit engine.

## How to apply

```
POST /api/proposals   {"preset": "legend_scalp"}
POST /api/proposals/{id}/confirm
```

Or on the phone: Settings → presets → "Legend Scalper (always trades)".

## Comparison (effective values)

| Setting | Both lanes | Hyper Scalp | Swing mode | Legend Scalper |
|---|---|---|---|---|
| Mode | both | scalp | swing | both |
| Loop seconds | 30 | 20 | 20 | 20 |
| **SCALP LANE** | | | | |
| Scalp on | on | on | off | on |
| Scalp min age (min) | 1 | 1 | 1 | 0 |
| Scalp max age (min) | 60 | 60 | 60 | 1440 |
| Scalp mcap min | $10k | $10k | $10k | $5k |
| Scalp mcap max | $100k | $100k | $100k | $50M |
| Scalp liq min | $5k | $5k | $5k | $1k |
| Scalp liq max | $20k | $20k | $20k | $5M |
| Scalp 5m vol min | $500 | $500 | $500 | none |
| Scalp stop | 5% | 5% | 5% | 8% |
| Scalp TP1 | 1.04x | 1.04x | 1.04x | 1.05x |
| Scalp TP2 | 1.08x | 1.08x | 1.08x | 1.12x |
| Scalp max hold (min) | 10 | 10 | 10 | 15 |
| **SWING LANE** | | | | |
| Swing on | on | off | on | on |
| Swing min age (min) | 60 | 60 | 60 | 0 |
| Swing max age (h) | 48 | 48 | 72 | 168 |
| Swing mcap min | $100k | $100k | $100k | $5k |
| Swing mcap max | $2M | $2M | $2M | $50M |
| Swing liq min | $15k | $15k | $15k | $2k |
| Swing 1h vol min | $10k | $10k | $10k | $1k |
| Swing 1h trades | 40 | 40 | 40 | 5 |
| **SIGNAL / MEME** | | | | |
| Signal confidence | 60 | 60 | 60 | 30 |
| Signal win prob | 0.52 | 0.52 | 0.52 | 0.30 |
| Require bullish | on | on | on | off |
| Wait for dip | on | off | off | off |
| Min holders | 150 | 150 | 150 | 20 |
| Analyst score | 6 | 6 | 6 | 3 |
| **RISK** | | | | |
| Max open slots | 6 | 6 | 6 | 10 |
| Daily loss stop | 3% | 3% | 3% | 10% |
| Resume after stop | off | off | off | on |

## What still blocks a trade (protective cuts)

Even at the floor, three cuts survive — verified by `tests/test_bands.py::LegendScalperTest`:

- mcap below $5k  -> `mcap`
- honeypot / no sells -> `no_sells`
- older than 7 days -> `age`

## The honest warning

"Always trades" is not "always wins". With an 8% stop and a 1.05x TP1, the
geometry only works if `tp1_frac × tp1 > round-trip cost`. On free 5m data the
median 5m range is ~0.65% against ~1% round-trip cost, so this preset will bleed
slowly on average. Watch `journal.jsonl` and cut it if the realised win rate
stays under ~45%.
