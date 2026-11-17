"""Single-instance local queue and per-member follow-up context."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DAY = 86400


class State:
    def __init__(self, path: Path):
        self.path = path
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, user TEXT NOT NULL, input TEXT, notice TEXT,
                stage TEXT NOT NULL DEFAULT 'queue', answer TEXT, model_answer TEXT,
                created INTEGER NOT NULL, retry_at INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0)""")
            if "archive_parent" not in {c["name"] for c in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN archive_parent TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS job_user ON jobs(user, created)")
            # A process may have stopped after DeepSeek accepted the request. Do not charge again.
            db.execute("""UPDATE jobs SET stage='reply', answer=?
                          WHERE stage='working'""", ("服务重启时上一条处理被中断，请核对飞书回复后再发送。",))
            db.execute("UPDATE jobs SET stage='reply' WHERE stage='sending'")
        self.prune()

    @contextmanager
    def db(self, *, exclusive=False):
        conn = sqlite3.connect(self.path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute("PRAGMA secure_delete=ON")
                if exclusive:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def prune(self):
        with self.db() as db:
            db.execute("DELETE FROM jobs WHERE created < ?", (time.time() - DAY,))

    def enqueue(self, ident, user, text, notice=None, *, archive_parent=None):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,user,input,notice,created,archive_parent) VALUES(?,?,?,?,?,?)",
                       (ident, user, text, notice, int(time.time()), archive_parent))

    def take(self):
        with self.db(exclusive=True) as db:
            row = db.execute("""SELECT j.* FROM jobs j WHERE j.stage IN ('queue','reply')
                AND j.retry_at <= ? AND NOT EXISTS (
                    SELECT 1 FROM jobs prior WHERE prior.user=j.user AND prior.rowid<j.rowid
                    AND prior.stage NOT IN ('done','failed')) ORDER BY j.rowid LIMIT 1""",
                             (time.time(),)).fetchone()
            if row:
                stage = "working" if row["stage"] == "queue" else "sending"
                db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, row["id"]))
        return row

    def history(self, user):
        with self.db() as db:
            rows = db.execute("""SELECT input,model_answer FROM jobs WHERE user=? AND stage='done'
                AND model_answer IS NOT NULL AND created>=? ORDER BY rowid DESC LIMIT 6""",
                              (user, time.time() - DAY,)).fetchall()
        messages, chars = [], 0
        for row in rows:
            size = len(row["input"]) + len(row["model_answer"])
            if chars + size > 20000:
                break
            messages[:0] = [{"role": "user", "content": row["input"]},
                            {"role": "assistant", "content": row["model_answer"]}]
            chars += size
        return messages

    def clear(self, user, ident):
        with self.db() as db:
            db.execute("""UPDATE jobs SET input=NULL,model_answer=NULL,answer=NULL,notice=NULL
                WHERE user=? AND rowid<(SELECT rowid FROM jobs WHERE id=?)""", (user, ident))

    def ready(self, ident, answer, model_answer=None):
        with self.db() as db:
            db.execute("""UPDATE jobs SET stage='reply',answer=?,model_answer=?,
                input=CASE WHEN ? IS NULL THEN NULL ELSE input END WHERE id=?""",
                       (answer, model_answer, model_answer, ident))

    def delivered(self, ident):
        with self.db() as db:
            db.execute("""UPDATE jobs SET stage='done',answer=NULL,notice=NULL,
                input=CASE WHEN model_answer IS NOT NULL THEN input END WHERE id=?""", (ident,))

    def retry(self, row):
        attempts = row["attempts"] + 1
        with self.db() as db:
            if attempts >= 7:
                db.execute("UPDATE jobs SET stage='failed',input=NULL,answer=NULL,model_answer=NULL WHERE id=?", (row["id"],))
            else:
                db.execute("UPDATE jobs SET stage='reply',attempts=?,retry_at=? WHERE id=?",
                           (attempts, time.time() + 2**attempts, row["id"]))
