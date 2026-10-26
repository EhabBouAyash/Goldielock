import argparse
import json

from deepseek_trial.v3_knowledge.cli import run_all
from deepseek_trial.v3_knowledge.cost import budget

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="v3 knowledge agents")
    parser.add_argument("command", nargs="?", default="cost", choices=("cost", "replay"))
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()
    if args.command == "cost":
        print(json.dumps(budget(), indent=2))
    else:
        run_all(audit=args.audit)
