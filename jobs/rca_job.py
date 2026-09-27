"""Post-incident RCA report generator — run as a Nebius Serverless Job.

Pulls resolved incidents from the agent API, asks Ultra for a blameless postmortem,
posts each report back to the agent (it shows up on the dashboard), prints it to the job log,
and also writes it to OUTPUT_DIR if that is set (e.g. a mounted volume).

Env: AGENT_URL, AGENT_TOKEN (if the agent requires one), NEBIUS_API_KEY, M_ULTRA, OUTPUT_DIR (optional)
"""
import json
import os
import pathlib

import requests
from openai import OpenAI

AGENT_URL = os.environ.get("AGENT_URL", "http://localhost:8080")
OUT = pathlib.Path(os.environ["OUTPUT_DIR"]) if os.environ.get("OUTPUT_DIR") else None
HEADERS = {"Authorization": f"Bearer {os.environ['AGENT_TOKEN']}"} if os.environ.get("AGENT_TOKEN") else {}
client = OpenAI(base_url=os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
                api_key=os.environ["NEBIUS_API_KEY"])

PROMPT = """Write a concise blameless postmortem in markdown with sections:
Summary, Impact, Timeline, Root cause, Resolution, What went well, Action items.
Use only facts in the incident record. Timestamps are unix seconds; render them as UTC times."""


def main():
    if OUT:
        OUT.mkdir(parents=True, exist_ok=True)
    incidents = requests.get(f"{AGENT_URL}/incidents", timeout=15).json()
    done = [i for i in incidents if i["status"] in ("remediated", "failed") and not i.get("postmortem")]
    for inc in done:
        resp = client.chat.completions.create(
            model=os.environ["M_ULTRA"], temperature=0.2, max_tokens=3000,
            messages=[{"role": "system", "content": PROMPT},
                      {"role": "user", "content": json.dumps(inc, default=str)[:30000]}])
        md = resp.choices[0].message.content or ""
        if "</think>" in md:
            md = md.split("</think>")[-1].strip()
        r = requests.post(f"{AGENT_URL}/incidents/{inc['id']}/postmortem", data=md.encode(),
                          headers={**HEADERS, "Content-Type": "text/markdown"}, timeout=15)
        print(f"--- postmortem {inc['id']} (posted: HTTP {r.status_code}) ---\n{md}\n")
        if OUT:
            (OUT / f"rca-{inc['id']}.md").write_text(md)
    print(f"{len(done)} resolved incidents processed")


if __name__ == "__main__":
    main()
