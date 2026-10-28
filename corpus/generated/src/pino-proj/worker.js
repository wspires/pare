// A thumbnail worker logging with pino (JSON lines). It takes jobs from an
// in-memory queue and fetches each source image from a real local HTTP
// server, which fails in chosen ways:
//   - for a stretch of jobs the server resets connections (ECONNRESET),
//     retried twice with warnings, then the job fails;
//   - one job's metadata has no `dimensions`, a TypeError in our code;
//   - a few fetches are slow (warnings).
// Seeded, and pino's time is fixed by a clock that advances per job.
import http from "node:http";
import pino from "pino";

let seed = 11;
const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
let now = Date.UTC(2026, 9, 7, 9, 0, 0);
const log = pino({
  base: { service: "thumbnailer", pid: 4242, hostname: "worker-1" },
  timestamp: () => `,"time":${now}`,
});

let resetting = false;
const server = http.createServer((req, res) => {
  if (resetting) {
    req.socket.destroy();
    return;
  }
  res.writeHead(200, { "content-type": "image/png" });
  res.end(Buffer.alloc(64));
});

function fetchImage(port, key) {
  return new Promise((resolve, reject) => {
    http
      .get({ port, path: `/images/${key}`, agent: false }, (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => resolve(Buffer.concat(chunks)));
      })
      .on("error", reject);
  });
}

function thumbSize(meta) {
  // The chosen bug: legacy uploads have no `dimensions`.
  const { width, height } = meta.dimensions;
  return [Math.round(width / 4), Math.round(height / 4)];
}

async function run(port) {
  log.info({ queue: "thumbs", concurrency: 4 }, "worker started");
  let done = 0, failed = 0;
  for (let i = 0; i < 1500; i++) {
    now += Math.floor(rand() * 300) + 40;
    resetting = i >= 900 && i < 915;
    const key = `u${Math.floor(rand() * 90000) + 10000}/${Math.floor(rand() * 1e9).toString(36)}.png`;
    const job = { id: 50000 + i, key };
    const jl = log.child({ jobId: job.id });
    const meta = i === 1200 ? { uploaded: "2019-03-02" } : { dimensions: { width: 1920, height: 1080 } };
    try {
      let img;
      for (let attempt = 1; ; attempt++) {
        try {
          img = await fetchImage(port, key);
          break;
        } catch (err) {
          if (attempt >= 3) throw err;
          jl.warn({ attempt, err: { code: err.code, message: err.message } }, "fetch failed, retrying");
        }
      }
      const [w, h] = thumbSize(meta);
      const ms = rand() < 0.02 ? Math.floor(rand() * 4000) + 1500 : Math.floor(rand() * 80) + 5;
      if (ms > 1000) jl.warn({ key, ms }, "slow fetch");
      jl.info({ key, bytes: img.length, w, h, ms }, "thumbnail written");
      done++;
    } catch (err) {
      failed++;
      jl.error({ err, key }, "job failed");
    }
    if (i % 500 === 499) log.info({ done, failed, queued: 1500 - i - 1 }, "progress");
  }
  log.info({ done, failed }, "queue drained, exiting");
}

server.listen(0, async () => {
  await run(server.address().port);
  server.close();
});
