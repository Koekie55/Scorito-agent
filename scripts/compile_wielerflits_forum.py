"""Compile curated WielerFlits forum claims into a bounded digest."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from scorito_agent.forum_opinion import compile_forum_opinion

def main()->None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("--output",type=Path,default=ROOT/"data"/"scorito"/"vuelta2026"/"wielerflits_forum_opinion.json")
    args=parser.parse_args()
    digest=compile_forum_opinion(json.loads(args.source.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(digest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Wrote {args.output}: {digest['summary']}")
if __name__=="__main__": main()