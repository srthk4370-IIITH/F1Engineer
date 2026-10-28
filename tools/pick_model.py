"""Race the OpenAI models on real radio questions (your recorded Silverstone session) and pick one.

  uv run python tools/pick_model.py                      # default candidates
  uv run python tools/pick_model.py gpt-6-luna gpt-6.1-sol
  uv run python tools/pick_model.py --write              # save the fastest working model to .env

Per model: time to first spoken sentence (what you feel on the radio), total time, tool used, and the answer.
"""
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from f1engineer.config import Config  # noqa: E402
from f1engineer.engine import Engine  # noqa: E402
from f1engineer.llm.agent import Agent, OpenAIProvider  # noqa: E402
from f1engineer.storage import Storage  # noqa: E402
from f1engineer.telemetry.capture import read_capture  # noqa: E402

CANDIDATES = ["gpt-6-luna", "gpt-6.1-terra", "gpt-6-sol", "gpt-5.4-mini", "gpt-4.1-mini"]
QUESTIONS = ["Where am I losing the most time?", "What happened at the Loop?",
             "Should I change my brake bias?", "Why did I lock up on my last lap?"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    write = "--write" in sys.argv
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("No OPENAI_API_KEY in .env - run: uv run f1engineer setup")
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    eng = Engine(cfg, storage=Storage(cfg))
    eng.speech.say = lambda *a: None
    for _, d in read_capture(ROOT / "data/captures/real_f1_2020_session1.f1cap"):
        eng.feed(d)
    eng.drain()

    results = []
    for model in args or CANDIDATES:
        print(f"\n=== {model}")
        firsts, totals = [], []
        try:
            prov = OpenAIProvider(model)
            for q in QUESTIONS:
                agent = Agent(eng.agent.tools, eng.agent.context_fn, sass=cfg.sass, provider=prov)
                t0 = time.perf_counter()
                first, out = None, []
                for s in agent.ask(q):
                    first = first or time.perf_counter() - t0
                    out.append(s)
                totals.append(time.perf_counter() - t0)
                firsts.append(first or totals[-1])
                print(f"  {firsts[-1]:.2f}s first / {totals[-1]:.2f}s total | {q}\n    -> {' '.join(out)}")
            print(f"  params accepted: {prov.kw}")
            results.append((statistics.median(firsts), statistics.median(totals), model))
        except Exception as e:
            print(f"  FAILED: {str(e)[:200]}")

    if not results:
        sys.exit("\nNo model worked - check the key / billing at https://platform.openai.com")
    print("\nmedian first-sentence latency (lower = snappier radio):")
    for f, t, m in sorted(results):
        print(f"  {m:16s} {f:.2f}s first  {t:.2f}s total")
    best = min(results)[2]
    print(f"\nfastest: {best}  (judge the answers above too - fastest is not always smartest)")
    if write:
        env = ROOT / ".env"
        lines = [ln for ln in env.read_text().splitlines() if not ln.startswith("OPENAI_MODEL=")]
        env.write_text("\n".join(lines + [f"OPENAI_MODEL={best}"]) + "\n")
        print(f"wrote OPENAI_MODEL={best} to .env")


if __name__ == "__main__":
    main()
