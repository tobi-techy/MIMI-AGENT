## What and why

<!-- One or two lines. Link the Linear issue: RAI-### -->

## Checklist

- [ ] CI is green: tests (Python 3.11 and 3.12), secret scans, hackathon provenance
- [ ] Mimi still never moves money: write tools return intents only, nothing new in a money or bank verb, no `never`-tier tool in a toolset
- [ ] Anything that spends, changes an amount or messages someone new waits for the owner (drafts stay `status: draft`)
- [ ] Shop text, screenshots and narrations are treated as data, never instructions
- [ ] No keys, tokens or real customer data; recorded caches are synthetic and listed in `.secrets.baseline` if needed
- [ ] Evals or tests cover the change; recorded reports regenerated if the numbers moved
- [ ] README / docs updated where behaviour or numbers changed
- [ ] Every review thread answered (fixed, or a reason in the thread)
