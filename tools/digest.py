#!/usr/bin/env python3
"""digest.py - compress research/*.json into a compact readable digest.

The raw research is ~400KB of JSON. This pulls out the signal: headline,
urgent actions, and findings (claim + implication, with detail truncated).

    python tools/digest.py              # full digest to stdout
    python tools/digest.py --urgent     # only headlines + urgent actions
    python tools/digest.py --detail 400 # allow longer detail excerpts
"""
import json, glob, os, argparse, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(HERE, "research")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urgent", action="store_true")
    ap.add_argument("--detail", type=int, default=240)
    ap.add_argument("--only", help="substring filter on filename")
    a = ap.parse_args()

    files = sorted(f for f in glob.glob(os.path.join(RES, "research__*.json")))
    if a.only:
        files = [f for f in files if a.only in os.path.basename(f)]
    if not files:
        print("no research files found in", RES)
        return 1

    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print("## %s -- UNREADABLE (%s)" % (os.path.basename(f), e))
            continue
        name = os.path.basename(f)[len("research__"):-len(".json")].replace("_", " ")
        print("\n" + "=" * 76)
        print("## " + name.upper())
        print("HEADLINE: " + str(d.get("headline", "")).strip())

        ua = d.get("urgent_actions") or []
        if ua:
            print("\nURGENT:")
            for u in ua:
                print("  ! " + str(u).strip())

        if a.urgent:
            continue

        fi = d.get("findings") or []
        order = {"high": 0, "medium": 1, "low": 2}
        fi = sorted(fi, key=lambda x: order.get(str(x.get("confidence", "low")).lower(), 3))
        print("\nFINDINGS (%d):" % len(fi))
        for x in fi:
            conf = str(x.get("confidence", "?"))[0].upper()
            claim = str(x.get("claim", "")).strip()
            det = " ".join(str(x.get("detail", "")).split())
            if len(det) > a.detail:
                det = det[: a.detail].rstrip() + "..."
            imp = " ".join(str(x.get("implication", "")).split())
            print("  [%s] %s" % (conf, claim))
            if det:
                print("      %s" % det)
            if imp:
                print("      => %s" % imp)

        sr = d.get("system_requirements") or []
        if sr:
            print("\nSYSTEM REQUIREMENTS:")
            for s in sr:
                print("  - " + " ".join(str(s).split()))

        rc = d.get("risky_claims") or []
        if rc:
            print("\nUNVERIFIED / RISKY:")
            for r in rc:
                print("  ? " + " ".join(str(r).split()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
