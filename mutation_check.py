"""Mutation check: the cursor tests must FAIL when the bug is reintroduced."""
import io
import subprocess
import sys
import unittest

from pathlib import Path

HERE = Path(__file__).parent
BOT = HERE / "bot.py"
ORIG = BOT.read_text(encoding="utf-8")

MUTANTS = [
    ("max -> min (the original bug)",
     'max(state["channels"][handle], msg.id)',
     'min(state["channels"][handle], msg.id)'),
    ("drop reversed() (descending order skips a failed send)",
     "for msg in reversed(msgs):",
     "for msg in msgs:"),
]


def run_suite() -> str:
    sys.path.insert(0, str(HERE))
    for name in list(sys.modules):
        if name in ("bot", "test_bot"):
            del sys.modules[name]
    import test_bot
    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=0)
    result = runner.run(unittest.defaultTestLoader.loadTestsFromModule(test_bot))
    return ("FAIL" if not result.wasSuccessful() else "OK") + " " + buf.getvalue().splitlines()[-1]


failures = 0
try:
    for label, old, new in MUTANTS:
        if old not in ORIG:
            print(f"SKIP {label}: pattern not found")
            failures += 1
            continue
        BOT.write_text(ORIG.replace(old, new), encoding="utf-8")
        outcome = run_suite()
        caught = outcome.startswith("FAIL")
        print(f"{'CAUGHT' if caught else 'MISSED'} {label}: {outcome}")
        if not caught:
            failures += 1
finally:
    BOT.write_text(ORIG, encoding="utf-8")

BOT.write_text(ORIG, encoding="utf-8")
print("restored; final =", run_suite())
sys.exit(1 if failures else 0)
