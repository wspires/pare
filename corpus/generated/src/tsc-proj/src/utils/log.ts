type Level = "debug" | "info" | "warn" | "error";

const order: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };

let threshold: Level = "info";

export function setLevel(level: Level): void {
  threshold = level;
}

export function log(level: Level, msg: string, extra?: Record<string, unknown>): void {
  if (order[level] < order[threshold]) {
    return;
  }
  const line = extra ? `${msg} ${JSON.stringify(extra)}` : msg;
  console[level](line);
}

export function timed<T>(label: string, fn: () => T): T {
  const start = performance.now();
  try {
    return fn();
  } finally {
    log("debug", `${label} took ${(performance.now() - start).toFixed(1)}ms`);
  }
}
