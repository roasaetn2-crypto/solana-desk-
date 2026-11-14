# Fix: risk budget locks the desk out of every entry

## Diagnosis
- Budget = 3% x $50 = $1.50; per-trade risk = 1.5% x $50 = $0.76
- split_across_slots was False, so 2 positions exceeded the budget and every later entry was refused.

## Done
- [x] config.py: split on, paper floor $0.20, added risk_budget_usd override
- [x] desk/risk.py: budget = max(daily stop, full-book risk) + explicit override + clearer message
- [x] desk/settings.py: Risk -> Open-risk budget + Split budget across slots
- [x] tests updated + 3 regression tests (19 risk tests pass, 734 total pass)
- [x] RISK-BUDGET-FIX.md v2 section
- [x] Commit + push to roasaetn2-crypto/solana-desk-

# Feature: tabbed dashboard (stop endless scrolling)

## Done
- [x] web/index.html: sticky Live / Control / Research tab bar + data-col tags
- [x] web/styles.css: pill tabs, sticky bar, mobile rule shows only active column
- [x] web/app.js: initDeskTabs() with localStorage persistence, arrow keys, count badges
- [x] fix Research badge selector (decision rows now carry data-mint)
- [x] verify: mobile shows exactly one column per tab, reload keeps tab, 737 tests pass
- [x] Commit 41f0e48 + push to roasaetn2-crypto/solana-desk- main
