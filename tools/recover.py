#!/usr/bin/env python3
"""
recover.py - Rebuild all completed research from workflow transcripts on disk.

WHY THIS EXISTS
The Workflow tool writes every completed agent's full structured result into
journal.jsonl as it lands. That file survives laptop shutdown, session loss and
usage-limit death. This script turns those raw journals back into readable JSON
so no research is ever lost, even in a brand-new Claude session.

USAGE
    python tools/recover.py            # write research/*.json + status table
    python tools/recover.py --status   # status table only, write nothing

Requires nothing but the standard library.
"""
import json, glob, os, sys, argparse, re

HOME = os.path.expanduser("~")
PROJECT_GLOB = os.path.join(
    HOME, ".claude", "projects", "C--Users-hp-Downloads-Projects-applier",
    "*", "subagents", "workflows", "wf_*",
)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research")

LABEL_PATTERNS = [
    (re.compile(r"research specialist for:\s*([A-Za-z0-9 ,&/+-]{2,60})"), "research"),
    (re.compile(r'fact-checker.{0,200}?researched "([A-Za-z0-9 ,&/+-]{2,60})"', re.S), "verify"),
    (re.compile(r"ASSIGNED DESIGN PHILOSOPHY:\s*([A-Za-z0-9 ,&/+-]{2,60})"), "design"),
    (re.compile(r"Score these three competing designs"), "judge"),
    (re.compile(r"You are the chief architect"), "synthesis"),
    (re.compile(r"You are a completeness critic"), "critique"),
]


def label_for_agent(tdir, agent_id):
    """Read the first record of an agent transcript and infer what it was doing."""
    path = os.path.join(tdir, "agent-%s.jsonl" % agent_id)
    if not os.path.exists(path):
        return ("unknown", agent_id)
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.readline()
    except OSError:
        return ("unknown", agent_id)
    for rx, kind in LABEL_PATTERNS:
        m = rx.search(head)
        if m:
            name = m.group(1).strip() if m.groups() else kind
            name = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip().replace(" ", "_")[:60]
            return (kind, name or kind)
    return ("unknown", agent_id)


def slug(kind, name):
    return ("%s__%s" % (kind, name)).lower()



def missing_dimensions():
    """Dimension keys from research/_dimensions.json that have no recovered result file."""
    spec = os.path.join(OUT_DIR, "_dimensions.json")
    if not os.path.exists(spec):
        return None
    with open(spec, encoding="utf-8") as fh:
        dims = json.load(fh).get("dimensions", [])
    have = {f.lower() for f in os.listdir(OUT_DIR)} if os.path.isdir(OUT_DIR) else set()
    out = []
    for d in dims:
        name = re.sub(r"[^A-Za-z0-9 _-]", "", d["label"]).strip().replace(" ", "_")[:60]
        fname = slug("research", name) + ".json"
        if fname.lower() not in have:
            out.append(d["key"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="print status only")
    ap.add_argument("--missing", action="store_true",
                    help="print dimension keys with no recovered result, one per line")
    ap.add_argument("--path-for", metavar="KEY",
                    help="print the exact destination file path for a dimension key")
    args = ap.parse_args()

    if args.path_for:
        spec = os.path.join(OUT_DIR, "_dimensions.json")
        with open(spec, encoding="utf-8") as fh:
            dims = json.load(fh).get("dimensions", [])
        for d in dims:
            if d["key"] == args.path_for:
                name = re.sub(r"[^A-Za-z0-9 _-]", "", d["label"]).strip().replace(" ", "_")[:60]
                print(os.path.join(OUT_DIR, slug("research", name) + ".json"))
                return 0
        print("unknown key: %s" % args.path_for, file=sys.stderr)
        return 2

    if args.missing:
        miss = missing_dimensions()
        if miss is None:
            print("research/_dimensions.json not found", file=sys.stderr)
            return 2
        for k in miss:
            print(k)
        return 0

    tdirs = sorted(glob.glob(PROJECT_GLOB))
    if not tdirs:
        print("No workflow transcript directories found under:\n  %s" % PROJECT_GLOB)
        return 1

    rows, recovered = [], 0
    for tdir in tdirs:
        journal = os.path.join(tdir, "journal.jsonl")
        if not os.path.exists(journal):
            continue
        latest = {}
        with open(journal, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                latest[d.get("key")] = d

        for key, d in latest.items():
            agent_id = d.get("agentId", "?")
            kind, name = label_for_agent(tdir, agent_id)
            status = d.get("type")
            payload = d.get("result")
            size = len(json.dumps(payload)) if payload is not None else 0
            rows.append((os.path.basename(tdir), kind, name, status, size))
            if status == "result" and payload is not None and not args.status:
                os.makedirs(OUT_DIR, exist_ok=True)
                dest = os.path.join(OUT_DIR, slug(kind, name) + ".json")
                # keep the biggest version if an agent was retried
                if os.path.exists(dest) and os.path.getsize(dest) >= size:
                    continue
                with open(dest, "w", encoding="utf-8", newline="") as out:
                    json.dump(payload, out, indent=2, ensure_ascii=False)
                recovered += 1

    rows.sort(key=lambda r: (r[1], r[2]))
    print("%-9s %-10s %-34s %-9s %s" % ("RUN", "KIND", "SUBJECT", "STATUS", "BYTES"))
    print("-" * 78)
    for run, kind, name, status, size in rows:
        mark = {"result": "OK", "failed": "FAILED", "started": "PENDING"}.get(status, status)
        print("%-9s %-10s %-34s %-9s %s" % (run[-8:], kind, name[:34], mark, size or ""))

    done = sum(1 for r in rows if r[3] == "result")
    print("-" * 78)
    print("completed=%d  failed=%d  pending=%d" % (
        done,
        sum(1 for r in rows if r[3] == "failed"),
        sum(1 for r in rows if r[3] == "started"),
    ))
    if not args.status:
        print("wrote %d JSON file(s) to %s" % (recovered, OUT_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
