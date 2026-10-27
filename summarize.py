"""python summarize.py results.jsonl [more.jsonl ...] -> per task/mode outcomes, median time, breakdown, tokens."""

import json
import statistics
import sys
from collections import defaultdict

groups = defaultdict(list)
for path in sys.argv[1:]:
    with open(path) as file:
        for line in file:
            row = json.loads(line)
            groups[(row["task"], row["mode"])].append(row)

for (task, mode), rows in groups.items():
    outcomes = defaultdict(int)
    for row in rows:
        outcomes[row.get("outcome", "error")] += 1
    times = [row["exec_ms"] / 1000 for row in rows if "exec_ms" in row]
    breakdown = defaultdict(list)
    for row in rows:
        for category, ms in row.get("breakdown_ms", {}).items():
            breakdown[category].append(ms)
    parts = " ".join(f"{k}={statistics.median(v) / 1000:.1f}" for k, v in sorted(breakdown.items()))
    tokens = statistics.median([row.get("tokens", 0) for row in rows])
    print(
        f"{task:9} {mode:6} {dict(outcomes)} median={statistics.median(times) if times else 0:5.1f}s "
        f"[{parts}] tokens={tokens:.0f}"
    )
