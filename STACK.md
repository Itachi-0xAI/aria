# The AI Trust Stack

Three open-source tools. Three different failure modes. One coherent answer to the question: *"Can we trust what our AI said, and why?"*

```
┌──────────────────────────────────────────────────────────────────┐
│  ARIA — Knowledge Integrity Layer                                │
│  "Is my AI's knowledge current vs. my live data?"                │
│  Detects retrieval index drift → injects correct context         │
│  → traces root cause → quantifies dollar exposure                │
│  Best for: Data teams in regulated industries                    │
│  github.com/Itachi-0xAI/aria                                     │
└──────────────────────────────────────────────────────────────────┘
                              ↕
                ARIA detects drift →
                CoAgent debates the remediation

┌──────────────────────────────────────────────────────────────────┐
│  CoAgent — Decision Integrity Layer                              │
│  "Why did the AI recommend this, and who decided?"               │
│  Structured adversarial debate → typed evidence →                │
│  human arbitration → permanent decision record (ADR/v1)          │
│  Best for: Engineers, architects, security teams                 │
│  github.com/Itachi-0xAI/coagent                                  │
└──────────────────────────────────────────────────────────────────┘
                              ↕
                CoAgent debate claims →
                ConsistencyGuard wraps each claim

┌──────────────────────────────────────────────────────────────────┐
│  ConsistencyGuard — Output Integrity Layer                       │
│  "Is my AI saying the same thing it said last week?"             │
│  Real-time semantic drift detection → violation alerts →         │
│  reliability scoring → webhook integration                       │
│  Best for: Any team shipping LLM agents to production            │
│  github.com/Itachi-0xAI/consistencyguard                         │
└──────────────────────────────────────────────────────────────────┘
```

## What Each Tool Catches

| Failure mode | Tool that catches it | When it fires |
|---|---|---|
| AI answers from stale index | **ARIA** | Before response |
| AI contradicts itself over time | **ConsistencyGuard** | After response |
| AI decision has no reasoning trail | **CoAgent** | During decision |

## Use All Three Together

```python
# 1. ARIA ensures the context is current before the debate starts
from modules.lci.context_injector import LiveContextInjector

lci = LiveContextInjector()
result = lci.inject_and_prompt(
    "What is the current enterprise pricing tier?",
    domain="customer_segments",
)
current_context = result["injected_value"]   # verified Gold value

# 2. CoAgent runs a structured debate using that verified context
from coagent import CollabSession, modes

session = CollabSession(mode=modes.DEBATE)
session.inject(current_context)
session.add_agent(name="Tier-A Advocate", role="advocate")
session.add_agent(name="Tier-B Advocate", role="advocate")
session.add_human(name="Pricing Lead")

# 3. ConsistencyGuard wraps each debate claim to catch internal contradictions
from consistencyguard.proxy import guarded_call
# (integrated inside CoAgent's DebateOrchestrator)

result = await session.run_until_complete()
# Output: DecisionRecord with verified context, typed claims,
# consistency flags, and human-signed decision
```

## Why Three Tools, Not One

Each layer has a different failure model, a different audit trail, and a different audience:

- **ARIA** is a freshness check. It only fires when the retrieval index disagrees with the warehouse. Its audit trail is the causal chain from Gold record → dbt run → injection.
- **CoAgent** is a decision framework. It only runs when a choice needs structured reasoning. Its audit trail is the typed claims, evidence, and human signature.
- **ConsistencyGuard** is a semantic baseline check. It only fires when an output diverges from prior outputs to the same prompt. Its audit trail is the violation log keyed by `agent_id`.

You can adopt them independently. You can adopt them together. The composition is additive, not coupled.

## License

All three projects: MIT.
