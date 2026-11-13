# The FOMO seat (optional)

The desk can read FOMO's coin pages for one extra piece of evidence: what
traders actually wrote about a coin, whether the deployer is dumping, and
whether the coin page itself says you cannot get out.

It is **off by default**. With no key, every lookup returns `unknown`, spends
zero credits, and the desk behaves exactly as it did before. Nothing in this
seat can raise into the trading loop.

## Turn it on

```bash
cd ~/solana-desk
echo 'export FOMO_API_KEY=fapi_your_key_here' >> ~/.bashrc
source ~/.bashrc
pkill -f "[u]vicorn app:app"
./termux/start-desk.sh
```

Then check that the desk sees it:

```bash
curl -s localhost:8000/api/state | python3 -c \
  "import sys,json; print(json.dumps(json.load(sys.stdin)['integrations']['fomo'], indent=1))"
```

You want `"enabled": true`, `"key_present": true`, and `credits_left` near
250000.

## What it costs

| call | credits |
|---|---|
| warnings + devs (`level: "signals"`) | 500 per coin |
| thesis page (`level: "thesis"`) | +1,250 per coin |

The free tier is 250,000 credits/month, so `signals` is roughly 500 coins a
month and `thesis` roughly 140. That is why `signals` is the default and why
the thesis page is only bought for finalists.

Unchanged answers cost **0** credits (ETags are stored and replayed). A 503 from
upstream costs **0** credits and is retried. A monthly budget is enforced in
code *before* the request, with a 5,000 credit reserve so a burst cannot starve
the rest of the month.

## How it votes

It can only ever say **no**. It can never say yes on its own.

**Hard vetoes** (in `desk/devcheck.py`):

- `fomo_sell_disabled` — the coin page says selling is disabled
- `fomo_trading_disabled` — trading is disabled entirely
- `fomo_buy_disabled` — buying is disabled
- `fomo_transfer_fee` — a punitive transfer fee (a soft honeypot)
- `fomo_honeypot`
- `fomo_dev_dumping` — the deployer has realised $500+ of profit into his own launch
- `fomo_thesis_bearish` — the crowd scores 25/100 or worse **with medium or high confidence**

A veto is added to `hard_no` and can never be removed by any other seat.

**A bounded nudge** (in `desk/meme.py`): the thesis score moves the *community*
part of the scorecard by at most **±8 points**, and only when confidence is
medium or high. It does not add a sixth weighted part, so the five weights still
sum to 1.0 and the chain still outvotes the crowd.

## Why the sentiment is not a keyword counter

Real FOMO thesis text looks like this:

```
"Free"                                    "Sending"
"big bossy"                               "ChatGPT said this should skyrocket"
"go up please bro this is everything :sob:"
```

A naive counter reads the last one as a strong buy. It is a bagholder pleading.
So the scorer:

- discounts pleading (`please`, `bro`, `come on`) by 65%
- discounts irony (`lol`, `sure`, `trust me bro`, `chatgpt said`) by 85%
- damps anything under 3 words to 30%
- weights each author by realised PnL, trade size and likes, and halves the
  deployer's own thesis
- **shrinks the whole score toward 50** when fewer than 3 credible authors exist,
  and returns `None` (unknown) when nobody wrote anything

`equity` is always 0 on the token thesis route, so credibility comes from
`realizedPnlUsd`, `tradeUsd` and `likes` instead.

## Tuning

In `config.py`:

```python
FOMO = {
    "enabled": True,
    "level": "signals",              # or "thesis" to also score what traders wrote
    "bearish_kill_score": 25,
    "bearish_kill_confidence": ("medium", "high"),
    "max_nudge": 8,
    "thesis_only_for_finalists": True,
}
```

To watch it without spending anything, leave `FOMO_API_KEY` unset. To try the
thesis level cheaply, set `"level": "thesis"` and leave
`"thesis_only_for_finalists": True` — it will only buy a thesis page for a coin
that already cleared every other gate.

## Tests

```bash
DESK_OFFLINE=1 python3 -m pytest tests/test_fomo.py -q
```

31 tests, no key, no network. They prove the seat is inert by default, that an
unknown is never treated as a clean pass, that a veto can never be cleared, and
that the nudge is capped at ±8.
