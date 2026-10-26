import argparse

from deepseek_trial.cli import run_all

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GOLD-OCT26-v1 six-book paper replay")
    parser.add_argument("--audit", action="store_true", help="Call DeepSeek TAKE/PASS on each candidate")
    args = parser.parse_args()
    run_all(audit=args.audit)
