#!/usr/bin/env python3
"""Generate the synthetic corpus files (deterministic; seeded).

    python bench/gen_synthetic.py corpus/

Writes <name>.log, <name>.keep and <name>.cmd for each synthetic case. The
real-tool logs in corpus/ (pytest, go test, cargo, make, npm) were captured
from actual runs and are not regenerated here.
"""

import os
import random
import sys


def write(out_dir, name, log_lines, keep_lines, cmd):
    with open(os.path.join(out_dir, f"{name}.log"), "w") as f:
        f.write("\n".join(log_lines) + "\n")
    with open(os.path.join(out_dir, f"{name}.keep"), "w") as f:
        f.write("\n".join(keep_lines) + "\n")
    with open(os.path.join(out_dir, f"{name}.cmd"), "w") as f:
        f.write(cmd + "\n")


def server_log(out_dir):
    """A service log: 6k lines of INFO request spam, periodic health checks,
    a WARN burst, a DB outage with stack traces, then recovery."""
    rng = random.Random(20261007)
    lines, keep = [], []
    t = 0
    paths = ["/api/v1/users", "/api/v1/orders", "/api/v1/cart", "/healthz", "/api/v1/products", "/metrics"]
    workers = [f"worker-{i}" for i in range(8)]

    def ts():
        nonlocal t
        t += rng.randint(1, 40)
        s = t // 1000
        return f"2026-10-07T09:{(s // 60) % 60:02d}:{s % 60:02d}.{t % 1000:03d}Z"

    def req():
        p = rng.choice(paths)
        ms = rng.randint(2, 180)
        rid = "%08x" % rng.getrandbits(32)
        return f"{ts()} INFO  [{rng.choice(workers)}] http: {rng.choice(['GET', 'GET', 'GET', 'POST', 'PUT'])} {p}?page={rng.randint(1, 40)} 200 {ms}ms rid={rid} ip=10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"

    lines.append("2026-10-07T09:00:00.000Z INFO  [main] starting inventory-api v2.14.3 (git 9f1c2ab) pid=4182")
    lines.append("2026-10-07T09:00:00.012Z INFO  [main] config: db=postgres://db-primary.internal:5432/inv pool=20 cache=redis://cache:6379")
    lines.append("2026-10-07T09:00:00.140Z INFO  [main] listening on 0.0.0.0:8080")
    keep.append("starting inventory-api v2.14.3 (git 9f1c2ab) pid=4182")
    for i in range(2200):
        lines.append(req())
        if i % 25 == 0:
            lines.append(f"{ts()} DEBUG [health] probe ok latency=1ms")
        if i % 400 == 0 and i:
            lines.append(f"{ts()} INFO  [gc] collected {rng.randint(1000, 90000)} objects in {rng.randint(3, 60)}ms heap={rng.randint(100, 900)}MB")
    # warn burst: cache slow
    for i in range(40):
        lines.append(req())
        lines.append(f"{ts()} WARN  [cache] redis GET slow: {rng.randint(300, 900)}ms key=product:{rng.randint(1000, 9999)} (threshold 250ms)")
    keep.append("WARN  [cache] redis GET slow")
    for i in range(600):
        lines.append(req())
        if i % 25 == 0:
            lines.append(f"{ts()} DEBUG [health] probe ok latency=1ms")
    # db outage
    outage_start = ts()
    lines.append(f"{outage_start} ERROR [db] connection to db-primary.internal:5432 lost: read tcp 10.4.1.17:53022->10.4.2.5:5432: connection reset by peer")
    keep.append("ERROR [db] connection to db-primary.internal:5432 lost: read tcp 10.4.1.17:53022->10.4.2.5:5432: connection reset by peer")
    for i in range(180):
        rid = "%08x" % rng.getrandbits(32)
        lines.append(f"{ts()} ERROR [{rng.choice(workers)}] http: GET {rng.choice(paths[:3])} 500 {rng.randint(1000, 5100)}ms rid={rid} err=\"pool exhausted: timed out waiting for connection after 5000ms\"")
        if i % 30 == 0:
            lines.append(f"{ts()} ERROR [{rng.choice(workers)}] unhandled exception in handler OrdersHandler.list")
            lines.append("    psycopg.OperationalError: connection failed: server closed the connection unexpectedly")
            lines.append("        at inventory/db/pool.py:212 in acquire")
            lines.append("        at inventory/db/pool.py:188 in _checkout")
            lines.append("        at inventory/handlers/orders.py:57 in list")
            lines.append("        at inventory/http/router.py:140 in dispatch")
            lines.append("        at inventory/http/server.py:88 in handle_request")
        if i % 7 == 0:
            lines.append(f"{ts()} WARN  [db] reconnect attempt {i // 7 + 1}/30 to db-primary.internal:5432 failed: dial tcp: connection refused")
    keep.append("psycopg.OperationalError: connection failed: server closed the connection unexpectedly")
    keep.append("at inventory/handlers/orders.py:57 in list")
    keep.append('err="pool exhausted: timed out waiting for connection after 5000ms"')
    lines.append(f"{ts()} FATAL [db] giving up after 30 reconnect attempts; circuit breaker OPEN for 60s")
    keep.append("FATAL [db] giving up after 30 reconnect attempts; circuit breaker OPEN for 60s")
    lines.append(f"{ts()} INFO  [db] reconnected to db-primary.internal:5432 (pool 20/20 healthy)")
    keep.append("INFO  [db] reconnected to db-primary.internal:5432 (pool 20/20 healthy)")
    for i in range(1500):
        lines.append(req())
        if i % 25 == 0:
            lines.append(f"{ts()} DEBUG [health] probe ok latency=1ms")
    lines.append(f"{ts()} INFO  [main] received SIGTERM, draining 12 in-flight requests")
    lines.append(f"{ts()} INFO  [main] shutdown complete exit=0")
    keep.append("shutdown complete exit=0")
    write(out_dir, "server-outage", lines, keep, "docker logs inventory-api")


def jest_log(out_dir):
    """A Jest run over 38 suites, 3 of which fail, with assertion diffs."""
    rng = random.Random(42)
    lines, keep = [], []
    suites = [f"src/{d}/{n}.test.ts" for d in ["components", "hooks", "utils", "api", "store"] for n in
              ["Button", "Modal", "Table", "Form", "useFetch", "useAuth", "format", "parse"]][:38]
    fails = {"src/utils/parse.test.ts", "src/api/useAuth.test.ts", "src/store/Table.test.ts"}
    total_t = 0
    passed = failed = 0
    for s in suites:
        if s in fails:
            lines.append(f"FAIL {s}")
        else:
            lines.append(f"PASS {s} ({rng.randint(100, 2400) / 1000:.3f} s)")
        lines.append(f"  {s.split('/')[-1].replace('.test.ts', '')}")
        n = rng.randint(4, 22)
        for i in range(n):
            ms = rng.randint(1, 90)
            name = rng.choice(["renders", "handles click", "validates input", "formats date", "parses empty", "retries on 500",
                               "updates state", "memoizes", "throws on invalid", "supports dark mode"]) + f" #{i}"
            if s in fails and i == n - 2:
                lines.append(f"    ✕ {name} ({ms} ms)")
                failed += 1
            else:
                lines.append(f"    ✓ {name} ({ms} ms)")
                passed += 1
            total_t += 1
        if s == "src/components/Modal.test.ts":
            lines.append("    ○ skipped closes on escape")
        lines.append("")
        if s in fails:
            lines.append(f"  ● {s.split('/')[-1].replace('.test.ts', '')} › {name}")
            lines.append("")
            if "parse" in s:
                lines.append("    expect(received).toEqual(expected) // deep equality")
                lines.append("")
                lines.append("    - Expected  - 1")
                lines.append("    + Received  + 1")
                lines.append("")
                lines.append("      Object {")
                lines.append('        "amount": 1250,')
                lines.append('    -   "currency": "USD",')
                lines.append('    +   "currency": undefined,')
                lines.append("      }")
                lines.append("")
                lines.append("      41 |   it('parses currency', () => {")
                lines.append("      42 |     const out = parseAmount('$1,250.00');")
                lines.append("    > 43 |     expect(out).toEqual({ amount: 1250, currency: 'USD' });")
                lines.append("         |                 ^")
                lines.append("      44 |   });")
                lines.append("")
                lines.append("      at Object.<anonymous> (src/utils/parse.test.ts:43:17)")
                keep.append('+   "currency": undefined,')
                keep.append("at Object.<anonymous> (src/utils/parse.test.ts:43:17)")
            elif "useAuth" in s:
                lines.append("    TypeError: Cannot read properties of undefined (reading 'token')")
                lines.append("")
                lines.append("      17 |   const { data } = useFetch('/session');")
                lines.append("    > 18 |   return data.token;")
                lines.append("         |               ^")
                lines.append("      19 | }")
                lines.append("")
                lines.append("      at useAuth (src/api/useAuth.ts:18:15)")
                lines.append("      at TestComponent (src/api/useAuth.test.ts:9:21)")
                lines.append("      at renderWithHooks (node_modules/react-dom/cjs/react-dom.development.js:16305:18)")
                lines.append("      at mountIndeterminateComponent (node_modules/react-dom/cjs/react-dom.development.js:20074:13)")
                lines.append("      at beginWork (node_modules/react-dom/cjs/react-dom.development.js:21587:16)")
                keep.append("TypeError: Cannot read properties of undefined (reading 'token')")
                keep.append("at useAuth (src/api/useAuth.ts:18:15)")
            else:
                lines.append("    thrown: \"Exceeded timeout of 5000 ms for a test.")
                lines.append("    Add a timeout value to this test to increase the timeout, if this is a long-running test. See https://jestjs.io/docs/api#testname-fn-timeout.\"")
                lines.append("")
                lines.append("      88 |")
                lines.append("    > 89 |   it('loads 10k rows', async () => {")
                lines.append("         |   ^")
                lines.append("      90 |     await store.load(rows);")
                lines.append("")
                lines.append("      at src/store/Table.test.ts:89:3")
                keep.append("Exceeded timeout of 5000 ms for a test.")
                keep.append("at src/store/Table.test.ts:89:3")
            lines.append("")
    lines.append("Summary of all failing tests")
    for s in sorted(fails):
        lines.append(f" FAIL  {s}")
    lines.append("")
    lines.append(f"Test Suites: {len(fails)} failed, {len(suites) - len(fails)} passed, {len(suites)} total")
    lines.append(f"Tests:       {failed} failed, 1 skipped, {passed} passed, {total_t} total")
    lines.append("Snapshots:   0 total")
    lines.append("Time:        27.418 s")
    lines.append("Ran all test suites.")
    keep.append(f"Test Suites: {len(fails)} failed, {len(suites) - len(fails)} passed, {len(suites)} total")
    for s in sorted(fails):
        keep.append(f" FAIL  {s}")
    write(out_dir, "jest-fail", lines, keep, "jest")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "corpus"
    os.makedirs(out_dir, exist_ok=True)
    server_log(out_dir)
    jest_log(out_dir)


if __name__ == "__main__":
    main()
