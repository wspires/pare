"""A nightly ETL job logging with structlog's JSON renderer.

It loads invoice rows from CSV text in memory into SQLite (a real database,
in memory), and fails in chosen ways:
  - a stretch of rows carries a currency the rates table lacks
    (KeyError in our code, logged with its traceback, row skipped);
  - one batch violates a UNIQUE constraint (sqlite3.IntegrityError,
    batch retried row by row);
  - some batches are slow (warnings).
Seeded; the timestamps come from a clock that advances per batch.
"""

import csv
import datetime as dt
import io
import random
import sqlite3

import structlog

clock = dt.datetime(2026, 10, 7, 2, 0, 0, tzinfo=dt.timezone.utc)


def add_time(logger, method, event):
    event["timestamp"] = clock.isoformat().replace("+00:00", "Z")
    return event


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        add_time,
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger().bind(job="invoices-nightly")

RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27}


def to_usd(row):
    return round(float(row["amount"]) * RATES[row["currency"]], 2)


def main():
    global clock
    rng = random.Random(5)
    db = sqlite3.connect(":memory:")
    db.execute("create table invoices (id integer primary key, customer text, usd real)")
    log.info("job started", source="s3://billing-exports/2026-10-06.csv", batches=600)
    loaded = skipped = 0
    next_id = 1
    for b in range(600):
        clock += dt.timedelta(milliseconds=rng.randint(200, 900))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "customer", "amount", "currency"])
        for _ in range(25):
            cur = "CHF" if 300 <= b < 303 and rng.random() < 0.3 else rng.choice(["USD", "EUR", "GBP"])
            w.writerow([next_id, f"cust-{rng.randint(1000, 9999)}", f"{rng.uniform(5, 900):.2f}", cur])
            next_id += 1
        if b == 450:
            next_id -= 3  # the chosen bug: an export overlaps the previous one
        rows = []
        for row in csv.DictReader(io.StringIO(buf.getvalue())):
            try:
                rows.append((int(row["id"]), row["customer"], to_usd(row)))
            except KeyError:
                skipped += 1
                log.exception("row skipped", batch=b, row_id=row["id"], currency=row["currency"])
        try:
            with db:
                db.executemany("insert into invoices values (?, ?, ?)", rows)
        except sqlite3.IntegrityError:
            log.exception("batch insert failed, retrying row by row", batch=b, rows=len(rows))
            for r in rows:
                try:
                    with db:
                        db.execute("insert into invoices values (?, ?, ?)", r)
                except sqlite3.IntegrityError as e:
                    skipped += 1
                    log.warning("duplicate invoice", batch=b, row_id=r[0], error=str(e))
        loaded += len(rows)
        ms = rng.randint(30, 120) if rng.random() > 0.05 else rng.randint(2000, 6000)
        (log.warning if ms > 1000 else log.info)("batch loaded" if ms <= 1000 else "slow batch", batch=b, rows=len(rows), ms=ms)
    total = db.execute("select count(*), round(sum(usd), 2) from invoices").fetchone()
    log.info("job finished", loaded=total[0], skipped=skipped, usd_total=total[1])


if __name__ == "__main__":
    main()
