"""SQLite access for the disc catalogue.

Plain sqlite3 and hand-written SQL. The dataset is at most 100 rows, so nothing
here needs to be clever - readability wins.
"""

import os
import sqlite3
from datetime import datetime
from importlib import resources

from ..config import Config
from ..slots import SLOT_MAX, SLOT_MIN
from . import taxonomy

#: Only a fallback: the application passes an explicit path from its Config.
DEFAULT_PATH = Config().db_path

STORED = "stored"
OUT = "out"

PER_PAGE = 12  # the PS3 has little memory; do not render 100 thumbnails at once
PER_PAGE_LIST = 25


def _now():
    return datetime.now().isoformat(timespec="seconds")


def connect(path=None):
    conn = sqlite3.connect(path or DEFAULT_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path=None):
    """Create the schema if it is not there yet. Safe to call repeatedly."""
    target = path or DEFAULT_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    ddl = resources.files(__package__).joinpath("schema.sql").read_text()
    conn = connect(target)
    # Migrate first: schema.sql indexes columns that an older database has yet
    # to gain, and CREATE INDEX on a missing column fails the whole script.
    _migrate(conn)
    with conn:
        conn.executescript(ddl)
    conn.close()


def _migrate(conn):
    """Bring a database created by an older schema up to the current one.

    schema.sql is all CREATE TABLE IF NOT EXISTS, so an existing database never
    gains a column added later - it is skipped whole and the mismatch only
    surfaces as a query error much later.

    The one migration so far replaces `media_type` (CD / DVD / Blu-ray / Data,
    a physical format) with `category` + `platform` (what is on the disc). Only
    Data carries over, as Software; the rest described the plastic and say
    nothing about the content, so those discs come out unclassified rather than
    guessed at.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(disc)")}
    if not cols or "category" in cols:
        return  # a fresh database - schema.sql builds it - or already migrated

    with conn:
        conn.execute("ALTER TABLE disc ADD COLUMN category TEXT")
        conn.execute("ALTER TABLE disc ADD COLUMN platform TEXT")
        if "media_type" in cols:
            conn.execute("UPDATE disc SET category = ? WHERE media_type = 'Data'",
                         ("Software",))
            try:
                conn.execute("ALTER TABLE disc DROP COLUMN media_type")
            except sqlite3.OperationalError:
                pass  # SQLite older than 3.35; the dead column is harmless


# -- reads ---------------------------------------------------------------

def _where(query=None, status=None, category=None, platform=None):
    """The WHERE tail shared by the catalogue page and its total, so the count
    above a filtered page can never describe a different set than the page."""
    sql, args = "", []
    if query:
        sql += (" AND (title LIKE ? OR subtitle LIKE ? OR notes LIKE ?"
                " OR category LIKE ? OR platform LIKE ?)")
        args += ["%%%s%%" % query] * 5
    if status:
        sql += " AND status = ?"
        args.append(status)
    if category:
        sql += " AND category = ?"
        args.append(category)
    if platform:
        sql += " AND platform = ?"
        args.append(platform)
    return sql, args


def count_discs(conn, query=None, status=None, category=None, platform=None):
    where, args = _where(query, status, category, platform)
    return conn.execute(
        "SELECT COUNT(*) FROM disc WHERE 1=1" + where, args).fetchone()[0]


def list_discs(conn, page=1, per_page=PER_PAGE, query=None, status=None,
               order="slot", category=None, platform=None):
    where, args = _where(query, status, category, platform)
    sql = "SELECT * FROM disc WHERE 1=1" + where
    sql += " ORDER BY %s" % ("title COLLATE NOCASE" if order == "title" else "slot")
    sql += " LIMIT ? OFFSET ?"
    args += [per_page, (max(page, 1) - 1) * per_page]
    return conn.execute(sql, args).fetchall()


def get_disc(conn, disc_id):
    return conn.execute("SELECT * FROM disc WHERE id = ?", (disc_id,)).fetchone()


def get_by_slot(conn, slot):
    return conn.execute("SELECT * FROM disc WHERE slot = ?", (slot,)).fetchone()


def occupied_slots(conn):
    return {row["slot"] for row in conn.execute("SELECT slot FROM disc")}


def next_free_slot(conn):
    """Lowest unoccupied slot, or None when the carousel is full."""
    taken = occupied_slots(conn)
    for slot in range(SLOT_MIN, SLOT_MAX + 1):
        if slot not in taken:
            return slot
    return None


def free_slots(conn):
    taken = occupied_slots(conn)
    return [s for s in range(SLOT_MIN, SLOT_MAX + 1) if s not in taken]


def recent_events(conn, limit=50):
    return conn.execute(
        "SELECT e.*, d.title FROM event e LEFT JOIN disc d ON d.id = e.disc_id"
        " ORDER BY e.at DESC, e.id DESC LIMIT ?", (limit,)).fetchall()


def category_counts(conn):
    """How many discs in each category, for the filter bar. Unclassified discs
    are counted by nothing, so they appear only under "All"."""
    return {row["category"]: row["n"] for row in conn.execute(
        "SELECT category, COUNT(*) AS n FROM disc"
        " WHERE category IS NOT NULL GROUP BY category")}


def platform_counts(conn):
    """Same, per console, within the games. Drives the second filter row."""
    return {row["platform"]: row["n"] for row in conn.execute(
        "SELECT platform, COUNT(*) AS n FROM disc"
        " WHERE category = ? AND platform IS NOT NULL GROUP BY platform",
        (taxonomy.GAMES,))}


def stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM disc").fetchone()[0]
    out = conn.execute(
        "SELECT COUNT(*) FROM disc WHERE status = ?", (OUT,)).fetchone()[0]
    return {"total": total, "out": out, "free": SLOT_MAX - total}


# -- writes --------------------------------------------------------------

def log_event(conn, kind, disc_id=None, slot=None, detail=None):
    conn.execute(
        "INSERT INTO event (disc_id, slot, kind, detail, at) VALUES (?,?,?,?,?)",
        (disc_id, slot, kind, detail, _now()))


def create_disc(conn, slot, title, subtitle=None, category=None, platform=None,
                notes=None, art_path=None, kind="added"):
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO disc (slot, title, subtitle, category, platform, notes,"
            " art_path, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slot, title, subtitle, category, platform, notes, art_path, STORED,
             now, now))
        log_event(conn, kind, cur.lastrowid, slot, title)
    return cur.lastrowid


def update_disc(conn, disc_id, **fields):
    allowed = ("title", "subtitle", "category", "platform", "notes", "art_path",
               "slot")
    sets, args = [], []
    for key in allowed:
        if key in fields:
            sets.append("%s = ?" % key)
            args.append(fields[key])
    if not sets:
        return
    sets.append("updated_at = ?")
    args += [_now(), disc_id]
    with conn:
        conn.execute("UPDATE disc SET %s WHERE id = ?" % ", ".join(sets), args)


def mark_out(conn, disc_id):
    now = _now()
    with conn:
        conn.execute(
            "UPDATE disc SET status = ?, checked_out_at = ?, updated_at = ?"
            " WHERE id = ?", (OUT, now, now, disc_id))
        row = get_disc(conn, disc_id)
        log_event(conn, "ejected", disc_id, row["slot"] if row else None)


def mark_stored(conn, disc_id, kind="returned"):
    now = _now()
    with conn:
        conn.execute(
            "UPDATE disc SET status = ?, checked_out_at = NULL, updated_at = ?"
            " WHERE id = ?", (STORED, now, disc_id))
        row = get_disc(conn, disc_id)
        log_event(conn, kind, disc_id, row["slot"] if row else None)


def delete_disc(conn, disc_id):
    row = get_disc(conn, disc_id)
    with conn:
        conn.execute("DELETE FROM disc WHERE id = ?", (disc_id,))
        log_event(conn, "removed", None, row["slot"] if row else None,
                  row["title"] if row else None)
    return row
