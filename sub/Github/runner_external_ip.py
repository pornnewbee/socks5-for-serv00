#!/usr/bin/env python3

import os
import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "./actions_exit.db")
REPORT_TOKEN = os.getenv("REPORT_TOKEN", "")

app = FastAPI(title="GitHub Actions Exit IP Collector")


class TraceReport(BaseModel):
    ip: str | None = None
    colo: str | None = None
    loc: str | None = None
    tls: str | None = None
    http: str | None = None
    warp: str | None = None
    gateway: str | None = None
    rbi: str | None = None
    kex: str | None = None

    # GitHub Actions 自己附带的信息
    repository: str | None = None
    workflow: str | None = None
    run_id: str | None = None
    runner_os: str | None = None
    runner_arch: str | None = None


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,

            ip TEXT,
            colo TEXT,
            loc TEXT,
            tls TEXT,
            http TEXT,
            warp TEXT,
            gateway TEXT,
            rbi TEXT,
            kex TEXT,

            repository TEXT,
            workflow TEXT,
            run_id TEXT,
            runner_os TEXT,
            runner_arch TEXT
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reports_ip
        ON reports(ip)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reports_colo
        ON reports(colo)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reports_loc
        ON reports(loc)
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


def check_token(authorization: str | None):
    if not REPORT_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="REPORT_TOKEN is not configured"
        )

    expected = f"Bearer {REPORT_TOKEN}"

    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid token"
        )


@app.get("/")
def root():
    return {
        "service": "github-actions-exit-ip-collector",
        "status": "ok"
    }


@app.post("/api/report")
def report(
    data: TraceReport,
    authorization: str | None = Header(default=None)
):
    check_token(authorization)

    timestamp = datetime.now(timezone.utc).isoformat()

    conn = db()

    conn.execute("""
        INSERT INTO reports (
            timestamp,
            ip,
            colo,
            loc,
            tls,
            http,
            warp,
            gateway,
            rbi,
            kex,
            repository,
            workflow,
            run_id,
            runner_os,
            runner_arch
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        timestamp,
        data.ip,
        data.colo,
        data.loc,
        data.tls,
        data.http,
        data.warp,
        data.gateway,
        data.rbi,
        data.kex,
        data.repository,
        data.workflow,
        data.run_id,
        data.runner_os,
        data.runner_arch,
    ))

    conn.commit()
    report_id = conn.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    conn.close()

    return {
        "ok": True,
        "id": report_id,
        "ip": data.ip,
        "colo": data.colo,
        "loc": data.loc,
    }


@app.get("/api/stats")
def stats(
    authorization: str | None = Header(default=None)
):
    check_token(authorization)

    conn = db()

    total = conn.execute(
        "SELECT COUNT(*) AS count FROM reports"
    ).fetchone()["count"]

    ips = conn.execute("""
        SELECT
            ip,
            COUNT(*) AS count,
            GROUP_CONCAT(DISTINCT colo) AS colos,
            GROUP_CONCAT(DISTINCT loc) AS locations
        FROM reports
        WHERE ip IS NOT NULL
        GROUP BY ip
        ORDER BY count DESC
    """).fetchall()

    colos = conn.execute("""
        SELECT
            colo,
            COUNT(*) AS count
        FROM reports
        WHERE colo IS NOT NULL
        GROUP BY colo
        ORDER BY count DESC
    """).fetchall()

    countries = conn.execute("""
        SELECT
            loc,
            COUNT(*) AS count
        FROM reports
        WHERE loc IS NOT NULL
        GROUP BY loc
        ORDER BY count DESC
    """).fetchall()

    conn.close()

    return {
        "total_reports": total,
        "ips": [dict(x) for x in ips],
        "colos": [dict(x) for x in colos],
        "countries": [dict(x) for x in countries],
    }


@app.get("/api/reports")
def reports(
    limit: int = 100,
    authorization: str | None = Header(default=None)
):
    check_token(authorization)

    limit = max(1, min(limit, 1000))

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM reports
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    return {
        "reports": [dict(x) for x in rows]
    }
