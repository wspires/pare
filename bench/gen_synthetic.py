#!/usr/bin/env python3
"""Generate the synthetic corpus files (deterministic; seeded).

    python bench/gen_synthetic.py corpus/

Writes <name>.log, <name>.keep and <name>.cmd for each synthetic case. The
real-tool logs in corpus/ (pytest, go test, cargo, make, npm) were captured
from actual runs and are not regenerated here. android-logcat stands in
for a real `adb logcat` capture, which needs an emulator or device.
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


def android_logcat(out_dir):
    """`adb logcat -d -v threadtime` from an emulator session in which a
    notes app syncs, hits a database error, and crashes with a
    NullPointerException in a RecyclerView adapter. Threadtime format as
    Android's logprint writes it: "MM-DD HH:MM:SS.mmm", pid and tid
    right-aligned in five columns, the level letter, the tag padded to
    eight characters before the colon, and every line of a multi-line
    message (a stack trace) under its own full prefix. Around it, the
    emulator's usual noise, including errors that repeat all session."""
    rng = random.Random(20261013)
    lines, keep = [], []
    ms = [9 * 3600_000 + 12 * 60_000]
    APP, APP_PID = "com.example.notes", 4321
    SYS = 512  # system_server

    def at(pid, tid, level, tag, msg):
        ms[0] += rng.randint(0, 35)
        t = ms[0]
        stamp = f"10-13 {t // 3600_000:02d}:{t // 60_000 % 60:02d}:{t // 1000 % 60:02d}.{t % 1000:03d}"
        lines.append(f"{stamp} {pid:5d} {tid:5d} {level} {tag:<8}: {msg}")

    def multi(pid, tid, level, tag, text):
        for part in text.split("\n"):
            at(pid, tid, level, tag, part)

    def noise(n):
        for _ in range(n):
            r = rng.random()
            if r < 0.14:
                at(298, 298, "E", "netmgr", "Failed to open QEMU pipe 'qemud:network': Invalid argument")
            elif r < 0.26:
                at(441, 467, "E", "GnssHAL_GnssInterface", f"gnssSvStatusCb: b: input svInfo.flags is {rng.choice([8, 9, 13])}")
            elif r < 0.36:
                at(306, 306, "D", "EGL_emulation", f"eglMakeCurrent: 0x{rng.randrange(16**8):08x}: ver 3 0 (tinfo 0x{rng.randrange(16**8):08x})")
            elif r < 0.45:
                at(SYS, rng.randint(530, 1900), "I", "chatty", f"uid=1000(system) {rng.choice(['Binder:512_3', 'android.display', 'android.bg'])} identical {rng.randint(2, 40)} lines")
            elif r < 0.53:
                at(SYS, rng.randint(530, 1900), "W", "ActivityManager", f"Slow operation: {rng.randint(51, 400)}ms so far, now at startProcess: done updating pids map")
            elif r < 0.61:
                at(SYS, 541, "D", "ConnectivityService", f"NetworkAgentInfo [MOBILE (LTE) - {rng.randint(100, 120)}] validation {rng.choice(['passed', 'passed', 'partial'])}")
            elif r < 0.68:
                at(SYS, 566, "I", "BatteryStatsService", "In wakeup_callback: resumed from suspend")
            elif r < 0.75:
                at(1873, 1873, "D", "gralloc_ranchu", f"gralloc_alloc: Creating ashmem region of size {rng.choice([8294400, 3686400, 9216000])}")
            elif r < 0.82:
                at(2210, 2241, "I", "GoogleInputMethod", f"onFinishInput() : Dummy InputConnection bound")
            elif r < 0.88:
                at(SYS, 1440, "W", "AppOps", f"Noting op not finished: uid 10{rng.randint(100, 160)} pkg com.google.android.gms code 79 time={rng.randint(10**12, 2 * 10**12)} duration=0")
            elif r < 0.94:
                at(SYS, 539, "I", "WindowManager", f"WIN DEATH: Window{{{rng.randrange(16**7):07x} u0 Splash Screen com.google.android.apps.nexuslauncher}}")
            else:
                at(1655, 1679, "I", "zygote64", f"Background concurrent copying GC freed {rng.randint(9000, 40000)}({rng.randint(400, 2000)}KB) AllocSpace objects, {rng.randint(0, 6)}(120KB) LOS objects, 49% free, {rng.randint(3, 9)}MB/{rng.randint(6, 18)}MB, paused {rng.randint(20, 90)}us total {rng.randint(10, 60)}.{rng.randint(100, 999)}ms")

    noise(900)
    at(SYS, 1440, "I", "ActivityTaskManager", f"START u0 {{act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] flg=0x10200000 cmp={APP}/.ui.MainActivity}} from uid 10087")
    at(SYS, 600, "I", "ActivityManager", f"Start proc {APP_PID}:{APP}/u0a87 for pre-top-activity {{{APP}/{APP}.ui.MainActivity}}")
    at(APP_PID, APP_PID, "I", APP.split(".")[-1], "Late-enabling -Xcheck:jni")
    at(APP_PID, APP_PID, "W", APP.split(".")[-1], "Unexpected CPU variant for X86 using defaults: x86_64")
    at(APP_PID, APP_PID, "D", "NetworkSecurityConfig", "No Network Security Config specified, using platform default")
    multi(APP_PID, APP_PID, "D", "StrictMode", "StrictMode policy violation; ~duration=14 ms: android.os.strictmode.DiskReadViolation\n"
          "\tat android.os.StrictMode$AndroidBlockGuardPolicy.onReadFromDisk(StrictMode.java:1659)\n"
          "\tat android.app.SharedPreferencesImpl.awaitLoadedLocked(SharedPreferencesImpl.java:283)\n"
          "\tat android.app.SharedPreferencesImpl.getString(SharedPreferencesImpl.java:298)\n"
          f"\tat {APP}.data.Settings.lastSync(Settings.kt:21)\n"
          f"\tat {APP}.ui.MainActivity.onCreate(MainActivity.kt:34)")
    at(APP_PID, 4370, "D", "NoteSync", "starting sync, last=2026-10-12T22:41:07Z")
    sync_tid = 4370
    for page in range(1, 9):
        at(APP_PID, 4372, "I", "okhttp.OkHttpClient", f"--> GET https://api.example.com/v2/notes?page={page}&since=1760308867")
        noise(rng.randint(10, 30))
        at(APP_PID, 4372, "I", "okhttp.OkHttpClient", f"<-- 200 OK https://api.example.com/v2/notes?page={page}&since=1760308867 ({rng.randint(80, 400)}ms, {rng.randint(9, 30)}.{rng.randint(0, 9)}kB body)")
        for k in range(25):
            note = (page - 1) * 25 + k + 1
            if note == 117:
                at(APP_PID, sync_tid, "W", "NoteRepository", "note 117 has no 'updated' field, skipping")
                keep.append("W NoteRepository: note 117 has no 'updated' field, skipping")
                continue
            at(APP_PID, sync_tid, "D", "NoteSync", f"synced note {note} (rev {rng.randint(1, 30)})")
            if rng.random() < 0.3:
                noise(1)
        noise(rng.randint(20, 60))
    at(APP_PID, sync_tid, "E", "SQLiteLog", "(1) no such table: notes_v2 in \"INSERT INTO notes_v2(id,title,body,updated) VALUES (?,?,?,?)\"")
    keep.append("E SQLiteLog: (1) no such table: notes_v2")
    multi(APP_PID, sync_tid, "E", "SQLiteDatabase", "Error inserting body=Groceries for the week title=Groceries id=201 updated=1760390400\n"
          "android.database.sqlite.SQLiteException: no such table: notes_v2 (code 1 SQLITE_ERROR): , while compiling: INSERT INTO notes_v2(id,title,body,updated) VALUES (?,?,?,?)\n"
          "\tat android.database.sqlite.SQLiteConnection.nativePrepareStatement(Native Method)\n"
          "\tat android.database.sqlite.SQLiteConnection.acquirePreparedStatement(SQLiteConnection.java:1047)\n"
          "\tat android.database.sqlite.SQLiteSession.prepare(SQLiteSession.java:597)\n"
          "\tat android.database.sqlite.SQLiteDatabase.insertWithOnConflict(SQLiteDatabase.java:1786)\n"
          f"\tat {APP}.data.NoteDao.upsert(NoteDao.kt:47)\n"
          f"\tat {APP}.data.NoteRepository.save(NoteRepository.kt:88)\n"
          f"\tat {APP}.sync.NoteSync.run(NoteSync.kt:61)")
    keep.append("android.database.sqlite.SQLiteException: no such table: notes_v2 (code 1 SQLITE_ERROR)")
    at(APP_PID, sync_tid, "I", "NoteSync", "sync finished: 199 synced, 1 skipped, 1 failed")
    noise(120)
    at(APP_PID, 4402, "W", "OpenGLRenderer", "Failed to choose config with EGL_SWAP_BEHAVIOR_PRESERVED, retrying without...")
    at(APP_PID, APP_PID, "I", "Choreographer", "Skipped 48 frames!  The application may be doing too much work on its main thread.")
    multi(APP_PID, APP_PID, "E", "AndroidRuntime", "FATAL EXCEPTION: main\n"
          f"Process: {APP}, PID: {APP_PID}\n"
          f"java.lang.NullPointerException: Attempt to invoke virtual method 'java.lang.String {APP}.data.Note.getTitle()' on a null object reference\n"
          f"\tat {APP}.ui.NoteListAdapter.onBindViewHolder(NoteListAdapter.kt:58)\n"
          f"\tat {APP}.ui.NoteListAdapter.onBindViewHolder(NoteListAdapter.kt:21)\n"
          "\tat androidx.recyclerview.widget.RecyclerView$Adapter.onBindViewHolder(RecyclerView.java:7254)\n"
          "\tat androidx.recyclerview.widget.RecyclerView$Adapter.bindViewHolder(RecyclerView.java:7337)\n"
          "\tat androidx.recyclerview.widget.RecyclerView$Recycler.tryBindViewHolderByDeadline(RecyclerView.java:6194)\n"
          "\tat androidx.recyclerview.widget.RecyclerView$Recycler.tryGetViewHolderForPositionByDeadline(RecyclerView.java:6460)\n"
          "\tat androidx.recyclerview.widget.LinearLayoutManager.layoutChunk(LinearLayoutManager.java:1583)\n"
          "\tat androidx.recyclerview.widget.LinearLayoutManager.fill(LinearLayoutManager.java:1541)\n"
          "\tat androidx.recyclerview.widget.LinearLayoutManager.onLayoutChildren(LinearLayoutManager.java:668)\n"
          "\tat androidx.recyclerview.widget.RecyclerView.dispatchLayoutStep2(RecyclerView.java:4309)\n"
          "\tat androidx.recyclerview.widget.RecyclerView.onLayout(RecyclerView.java:4579)\n"
          "\tat android.view.View.layout(View.java:24529)\n"
          "\tat android.view.ViewGroup.layout(ViewGroup.java:7383)\n"
          "\tat android.view.ViewRootImpl.performLayout(ViewRootImpl.java:4422)\n"
          "\tat android.view.ViewRootImpl.performTraversals(ViewRootImpl.java:3828)\n"
          "\tat android.view.Choreographer.doFrame(Choreographer.java:1337)\n"
          "\tat android.os.Handler.dispatchMessage(Handler.java:106)\n"
          "\tat android.os.Looper.loop(Looper.java:317)\n"
          "\tat android.app.ActivityThread.main(ActivityThread.java:8705)\n"
          "\tat java.lang.reflect.Method.invoke(Native Method)\n"
          "\tat com.android.internal.os.RuntimeInit$MethodAndArgsCaller.run(RuntimeInit.java:580)\n"
          "\tat com.android.internal.os.ZygoteInit.main(ZygoteInit.java:886)")
    keep += ["E AndroidRuntime: FATAL EXCEPTION: main",
             f"Process: {APP}, PID: {APP_PID}",
             f"java.lang.NullPointerException: Attempt to invoke virtual method 'java.lang.String {APP}.data.Note.getTitle()' on a null object reference",
             f"at {APP}.ui.NoteListAdapter.onBindViewHolder(NoteListAdapter.kt:58)"]
    at(APP_PID, APP_PID, "I", "Process", f"Sending signal. PID: {APP_PID} SIG: 9")
    at(SYS, 1440, "W", "ActivityTaskManager", f"  Force finishing activity {APP}/.ui.MainActivity")
    at(SYS, 1912, "I", "ActivityManager", f"Process {APP} (pid {APP_PID}) has died: fg  TOP ")
    keep.append(f"Process {APP} (pid {APP_PID}) has died")
    at(SYS, 1912, "W", "InputDispatcher", f"channel '9f3c2a1 {APP}/{APP}.ui.MainActivity (server)' ~ Channel is unrecoverably broken and will be disposed!")
    noise(700)
    write(out_dir, "android-logcat", lines, keep, "adb logcat -d -v threadtime")


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "corpus"
    os.makedirs(out_dir, exist_ok=True)
    server_log(out_dir)
    jest_log(out_dir)
    ci_pipeline(out_dir)
    android_logcat(out_dir)


if __name__ == "__main__":
    main()
