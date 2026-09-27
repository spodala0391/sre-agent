"""Check each Nemotron tier before building on it.

  set -a; source .env; set +a; python scripts/smoke-test-models.py

For each of M_NANO / M_SUPER / M_ULTRA it checks:
  1. plain chat works
  2. the reply can be parsed as JSON (what triage/diagnosis rely on)
  3. (Super only) native tool calling works - the investigator depends on it
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from openai import OpenAI  # noqa: E402

from agent.llm import parse_json  # noqa: E402

c = OpenAI(base_url=os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
           api_key=os.environ["NEBIUS_API_KEY"])
TIERS = {"nano": os.getenv("M_NANO"), "super": os.getenv("M_SUPER"), "ultra": os.getenv("M_ULTRA")}


def extra(tier):
    raw = os.getenv(f"EXTRA_BODY_{tier.upper()}")
    return {"extra_body": json.loads(raw)} if raw else {}


ok = True
for tier, model in TIERS.items():
    if not model:
        print(f"[{tier}] M_{tier.upper()} not set"); ok = False; continue
    t0 = time.time()
    try:
        r = c.chat.completions.create(model=model, max_tokens=400, temperature=0, **extra(tier), messages=[
            {"role": "system", "content": 'Reply with ONLY JSON: {"verdict": "noise"|"investigate", "summary": "..."}'},
            {"role": "user", "content": "Alert: pod cartservice restarted 6 times in 3 minutes, last reason OOMKilled"}])
        txt = r.choices[0].message.content or ""
        parsed = parse_json(txt)
        print(f"[{tier}] {model}: {int((time.time()-t0)*1000)} ms, json={'OK' if parsed else 'FAIL'} -> {parsed or txt[:200]!r}")
        ok &= bool(parsed)
    except Exception as e:
        print(f"[{tier}] {model}: ERROR {e}"); ok = False

# tool calling on Super
tool = {"type": "function", "function": {"name": "get_pods", "description": "List pods in a namespace",
        "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": ["namespace"]}}}
try:
    r = c.chat.completions.create(model=TIERS["super"], max_tokens=400, temperature=0, tools=[tool],
                                  tool_choice="auto", **extra("super"), messages=[
        {"role": "user", "content": "Investigate crashing pods in namespace 'app'. Use the tools."}])
    tc = r.choices[0].message.tool_calls
    print(f"[super] tool calling: {'OK -> ' + tc[0].function.name + tc[0].function.arguments if tc else 'NO TOOL CALL (see README troubleshooting)'}")
    ok &= bool(tc)
except Exception as e:
    print(f"[super] tool calling ERROR {e}"); ok = False

print("\nALL GOOD" if ok else "\nFix the failures above before moving on.")
sys.exit(0 if ok else 1)
