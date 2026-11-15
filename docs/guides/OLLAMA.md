# Ollama on the desk

Ollama is the only AI provider here that is **free, unmetered and private**. It is
already wired into two places, and they are not equally safe.

## Where it is used

| Seat | File | Can it change a trade? |
|---|---|---|
| Advisory second opinion | `desk/brain_advisor.py` | **No.** Nothing in `risk.py`, `broker.py`, `live.py` or the scanner imports it. It only attaches an opinion to the decision record. |
| Analyst score (0-10) | `desk/brain.py` (`DESK_BRAIN=ollama`) | **Yes** - the score gates entries against `analyst_min_score`. |
| Desk AI chat / post-mortems | `desk/desk_chat.py` (via `ADVISOR.providers()`) | No. |

**Recommended split:** let Ollama serve the advisory layer and chat, and keep the
analyst score on Gemini/Groq. A 3B model scoring structured JSON is unreliable -
it either rejects everything or waves junk through, and you cannot see why.

## One variable, both seats

Both `brain_advisor.py` and `brain.py` resolve the server the same way:

```
OLLAMA_BASE_URL            # wins
OLLAMA_HOST                # the variable the ollama CLI sets
http://localhost:11434     # default
```

and the model:

```
BRAIN_OLLAMA_LOCAL_MODEL   # wins
OLLAMA_MODEL
llama3.2                   # default
```

Before 2026-11-15 `brain.py` read only `OLLAMA_HOST`, so setting `OLLAMA_BASE_URL`
(the documented variable, and the one the Termux script writes) left the analyst
seat pointed at localhost and silently falling back to the heuristic. Both seats
now read the same pair.

## Render cannot host Ollama

The free instance is ~512MB RAM with no GPU. `llama3.2` alone needs more than
that. Run the model on the phone or a home box and let Render reach it over HTTPS:

```bash
bash termux/ollama-tunnel.sh
# or: OLLAMA_MODEL=qwen2.5:7b bash termux/ollama-tunnel.sh
```

It starts `ollama serve`, pulls the model, opens a Cloudflare quick tunnel and
prints the two values to paste into **Render -> solana-desk-1 -> Environment**:

```
OLLAMA_BASE_URL           = https://<random>.trycloudflare.com
BRAIN_OLLAMA_LOCAL_MODEL  = llama3.2
```

A quick-tunnel URL is random and dies when the tunnel stops. For a stable
hostname use a named tunnel (see `deploy/oracle/cloudflared-setup.sh`).

## Checking it

```bash
python3 scripts/verify_ollama_local.py   # proves local mode end to end, no key
python3 scripts/ai_status.py             # per-provider quota, no calls made
```

`verify_ollama_local.py` starts a fake Ollama and runs a real review: it checks
detection, that local Ollama is tried first, that no `Authorization` header is
sent, and that cloud Ollama stays last.
