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


def ci_pipeline(out_dir):
    """A CI job log, ~20k lines / ~2 MB: npm ci chatter, a webpack build with
    one type error, a 600-suite Jest run with four failures, a docker build,
    and a Kubernetes rollout that never becomes ready."""
    rng = random.Random(7)
    lines, keep = [], []
    pkgs = [f"@scope/{w}" if i % 5 == 0 else w for i, w in enumerate(
        ["react", "lodash", "axios", "chalk", "debug", "ms", "semver", "glob", "minimatch", "yargs", "commander",
         "express", "body-parser", "cors", "uuid", "dayjs", "zod", "rxjs", "tslib", "typescript", "esbuild",
         "vite", "jest", "ts-jest", "babel-core", "eslint", "prettier", "husky", "lint-staged", "dotenv"])]

    def ts(sec):
        return f"2026-10-08T11:{sec // 60:02d}:{sec % 60:02d}.{rng.randint(0, 999):03d}Z"

    # 1. checkout + npm ci (verbose)
    lines += ["##[group]Run actions/checkout@v4", "Syncing repository: acme/storefront", "HEAD is now at 3e1f9a2 Merge pull request #4821",
              "##[endgroup]", "##[group]Run npm ci --loglevel verbose", "npm verb cli /opt/hostedtoolcache/node/22.11.0/x64/bin/node",
              "npm info using npm@10.9.0", "npm info using node@v22.11.0"]
    t = 0
    for i in range(3000):
        p = pkgs[i % len(pkgs)]
        ver = f"{rng.randint(0, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 30)}"
        kind = i % 4
        if kind == 0:
            lines.append(f"npm http fetch GET 200 https://registry.npmjs.org/{p} {rng.randint(20, 400)}ms (cache miss)")
        elif kind == 1:
            lines.append(f"npm http fetch GET 200 https://registry.npmjs.org/{p}/-/{p.split('/')[-1]}-{ver}.tgz {rng.randint(10, 900)}ms")
        elif kind == 2:
            lines.append(f"npm timing reifyNode:node_modules/{p} Completed in {rng.randint(5, 600)}ms")
        else:
            lines.append(f"npm verb reify extracting {p}@{ver} to node_modules/{p}")
        t += 1
    lines += ["npm warn deprecated inflight@1.0.6: This module is not supported, and leaks memory.",
              "npm warn deprecated glob@7.2.3: Glob versions prior to v9 are no longer supported",
              "", "added 1412 packages in 48s", "", "211 packages are looking for funding", "  run `npm fund` for details",
              "npm verb exit 0", "npm info ok", "##[endgroup]"]
    keep.append("added 1412 packages in 48s")

    # 2. webpack build
    lines += ["##[group]Run npm run build", "", "> storefront@3.8.1 build", "> webpack --mode production --progress", ""]
    for i in range(2200):
        mod = rng.choice(["src/components", "src/pages", "src/hooks", "src/api", "node_modules/react-dom/cjs",
                          "node_modules/lodash", "src/store", "src/utils"]) + f"/{rng.choice(['index', 'Button', 'Cart', 'Checkout', 'useFetch', 'format', 'slice', 'client'])}{rng.randint(0, 400)}.{rng.choice(['ts', 'tsx', 'js'])}"
        lines.append(f"[{i + 1}/2200] {int(100 * (i + 1) / 2200)}% building ./{mod}")
        if i == 1700:
            lines += ["WARNING in ./src/pages/Checkout.tsx 212:14-38",
                      "export 'applyCoupon' (imported as 'applyCoupon') was not found in '../api/client' (possible exports: createOrder, fetchCart)",
                      ""]
            keep.append("export 'applyCoupon' (imported as 'applyCoupon') was not found in '../api/client'")
    for i in range(60):
        lines.append(f"asset {rng.choice(['main', 'vendor', 'checkout', 'cart'])}.{'%08x' % rng.getrandbits(32)}.js {rng.randint(10, 2200)} KiB [emitted] [immutable] (name: {rng.choice(['main', 'vendor', 'checkout'])})")
    lines += ["", "ERROR in ./src/store/cartSlice.ts 88:31",
              "TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.",
              "  Type 'undefined' is not assignable to type 'string'.",
              "    86 |   const id = action.payload.id;",
              "    87 |   const line = state.lines.find(l => l.id === id);",
              "  > 88 |   state.selected = normalize(action.payload.sku);",
              "       |                               ^^^^^^^^^^^^^^^^^^",
              "    89 |   if (line) line.qty += 1;", "",
              "webpack 5.95.0 compiled with 1 error and 1 warning in 94213 ms",
              "##[endgroup]"]
    keep.append("TS2345: Argument of type 'string | undefined' is not assignable to parameter of type 'string'.")
    keep.append("webpack 5.95.0 compiled with 1 error and 1 warning in 94213 ms")

    # 3. jest: 600 suites
    lines += ["##[group]Run npm test -- --ci", "", "> storefront@3.8.1 test", "> jest --ci", ""]
    suites = [f"src/{d}/{n}{i}.test.tsx" for i in range(30) for d in ["components", "pages", "hooks", "api", "store"]
              for n in ["Button", "Cart", "Checkout", "useFetch"]][:600]
    fails = {"src/pages/Checkout7.test.tsx", "src/store/Cart19.test.tsx", "src/api/useFetch3.test.tsx", "src/hooks/useFetch24.test.tsx"}
    passed = failed = 0
    for s in suites:
        lines.append(f"FAIL {s}" if s in fails else f"PASS {s} ({rng.randint(80, 3900) / 1000:.3f} s)")
        n = rng.randint(6, 30)
        for i in range(n):
            name = rng.choice(["renders", "handles click", "validates input", "formats total", "retries on 500",
                               "updates state", "memoizes", "throws on invalid", "supports dark mode", "hydrates"]) + f" #{i}"
            if s in fails and i == n - 1:
                lines.append(f"  ✕ {name} ({rng.randint(1, 90)} ms)")
                failed += 1
            else:
                lines.append(f"  ✓ {name} ({rng.randint(1, 90)} ms)")
                passed += 1
        lines.append("")
        if s in fails:
            short = s.split("/")[-1].replace(".test.tsx", "")
            lines.append(f"  ● {short} › {name}")
            lines.append("")
            if "Checkout" in s:
                lines += ["    expect(received).toBe(expected) // Object.is equality", "",
                          "    Expected: \"$1,250.00\"", "    Received: \"$1250\"", "",
                          "      54 |     render(<Checkout cart={cart} />);",
                          "    > 55 |     expect(screen.getByTestId('total').textContent).toBe('$1,250.00');",
                          "         |                                                      ^",
                          "      56 |   });", "",
                          f"      at Object.<anonymous> ({s}:55:54)"]
                keep.append('Received: "$1250"')
                keep.append(f"at Object.<anonymous> ({s}:55:54)")
            elif "Cart19" in s:
                lines += ["    TypeError: Cannot read properties of undefined (reading 'sku')", "",
                          "      86 |   const id = action.payload.id;",
                          "    > 88 |   state.selected = normalize(action.payload.sku);",
                          "         |                                           ^", "",
                          "      at normalize (src/store/cartSlice.ts:88:43)",
                          f"      at Object.<anonymous> ({s}:31:12)"]
                keep.append("TypeError: Cannot read properties of undefined (reading 'sku')")
                keep.append("at normalize (src/store/cartSlice.ts:88:43)")
            else:
                lines += ["    thrown: \"Exceeded timeout of 5000 ms for a test.",
                          "    Add a timeout value to this test to increase the timeout, if this is a long-running test. See https://jestjs.io/docs/api#testname-fn-timeout.\"", "",
                          f"      at {s}:40:3"]
                keep.append(f"at {s}:40:3")
            lines.append("")
    lines += ["Summary of all failing tests"] + [f" FAIL  {s}" for s in sorted(fails)] + [
        "", f"Test Suites: {len(fails)} failed, {len(suites) - len(fails)} passed, {len(suites)} total",
        f"Tests:       {failed} failed, {passed} passed, {failed + passed} total", "Snapshots:   0 total",
        "Time:        412.8 s", "Ran all test suites.", "##[endgroup]"]
    keep.append(f"Tests:       {failed} failed, {passed} passed, {failed + passed} total")

    # 4. docker build + rollout
    lines += ["##[group]Run docker build -t registry.acme.io/storefront:3e1f9a2 .", "#0 building with \"default\" instance using docker driver", ""]
    step = 1
    for stage, cmd in enumerate(["FROM node:22-alpine AS build", "WORKDIR /app", "COPY package*.json ./", "RUN npm ci --omit=dev",
                                 "COPY . .", "RUN npm run build", "FROM nginx:1.27-alpine", "COPY --from=build /app/dist /usr/share/nginx/html"]):
        lines.append(f"#{step} [stage-{stage // 6} {stage % 6 + 1}/6] {cmd}")
        for j in range(rng.randint(2, 40)):
            lines.append(f"#{step} sha256:{'%064x' % rng.getrandbits(256)} {rng.randint(1, 90)}.{rng.randint(0, 99):02d}MB / {rng.randint(90, 200)}MB {rng.randint(1, 9)}.{rng.randint(0, 9)}s")
        lines.append(f"#{step} DONE {rng.randint(1, 120)}.{rng.randint(0, 9)}s")
        step += 1
    lines += ["#9 exporting to image", "#9 exporting layers done", "#9 writing image sha256:" + "%064x" % rng.getrandbits(256) + " done",
              "#9 naming to registry.acme.io/storefront:3e1f9a2 done", "#9 DONE 2.1s", "##[endgroup]",
              "##[group]Run kubectl set image deployment/storefront web=registry.acme.io/storefront:3e1f9a2 && kubectl rollout status deployment/storefront --timeout=300s",
              "deployment.apps/storefront image updated",
              "Waiting for deployment \"storefront\" rollout to finish: 1 out of 6 new replicas have been updated..."]
    for i in range(280):
        lines.append(f"Waiting for deployment \"storefront\" rollout to finish: {min(6, 1 + i // 60)} out of 6 new replicas have been updated...")
        if i % 35 == 10:
            lines += [f"{ts(1800 + i)}  Warning  Unhealthy  pod/storefront-7c9d4f5b8-{rng.choice(['x2k9p', 'q7m2z', 'b4nn1'])}  Readiness probe failed: HTTP probe failed with statuscode: 503",
                      f"{ts(1801 + i)}  Warning  BackOff    pod/storefront-7c9d4f5b8-{rng.choice(['x2k9p', 'q7m2z', 'b4nn1'])}  Back-off restarting failed container web in pod storefront-7c9d4f5b8"]
    lines += ["error: timed out waiting for the condition", "##[error]Process completed with exit code 1.", "##[endgroup]",
              "Post job cleanup.", "##[error]Deploy job failed after 31m 12s"]
    keep.append("Readiness probe failed: HTTP probe failed with statuscode: 503")
    keep.append("Back-off restarting failed container web in pod storefront-7c9d4f5b8")
    keep.append("error: timed out waiting for the condition")
    keep.append("##[error]Process completed with exit code 1.")
    write(out_dir, "ci-pipeline", lines, keep, "bash ci.sh")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "corpus"
    os.makedirs(out_dir, exist_ok=True)
    server_log(out_dir)
    jest_log(out_dir)
    ci_pipeline(out_dir)


if __name__ == "__main__":
    main()
