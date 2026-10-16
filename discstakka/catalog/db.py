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
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(disc)")}
    if not cols:
        return  # a fresh database; schema.sql builds it
    _migrate_category(conn, cols)
    _migrate_sets(conn, cols)


def _migrate_category(conn, cols):
    """Replace `media_type` (CD / DVD / Blu-ray / Data, a physical format) with
    `category` + `platform` (what is on the disc). Only Data carries over, as
    Software; the rest described the plastic and say nothing about the content,
    so those discs come out unclassified rather than guessed at.
    """
    if "category" in cols:
        return
    with conn:
        conn.execute("ALTER TABLE disc ADD COLUMN category TEXT")
        conn.execute("ALTER TABLE disc ADD COLUMN platform TEXT")
        if "media_type" in cols:
            conn.execute(
                "UPDATE disc SET category = ? WHERE media_type = 'Data'", ("Software",)
            )
            try:
                conn.execute("ALTER TABLE disc DROP COLUMN media_type")
            except sqlite3.OperationalError:
                pass  # SQLite older than 3.35; the dead column is harmless


def _migrate_sets(conn, cols):
    if "parent_id" in cols:
        return
    with conn:
        conn.execute(
            "ALTER TABLE disc ADD COLUMN parent_id INTEGER REFERENCES disc(id)"
        )
        conn.execute("ALTER TABLE disc ADD COLUMN disc_no INTEGER")


# -- reads ---------------------------------------------------------------


def _where(query=None, status=None, category=None, platform=None):
    """The WHERE tail shared by the catalogue page and its total, so the count
    above a filtered page can never describe a different set than the page.

    A set is listed once, by its lead, and matches a status when any of its
    discs has it."""
    sql, args = " AND parent_id IS NULL", []
    if query:
        sql += (
            " AND (title LIKE ? OR subtitle LIKE ? OR notes LIKE ?"
            " OR category LIKE ? OR platform LIKE ?)"
        )
        args += ["%%%s%%" % query] * 5
    if status:
        sql += (
            " AND EXISTS (SELECT 1 FROM disc m"
            " WHERE (m.id = disc.id OR m.parent_id = disc.id) AND m.status = ?)"
        )
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
    return conn.execute("SELECT COUNT(*) FROM disc WHERE 1=1" + where, args).fetchone()[
        0
    ]


def list_discs(
    conn,
    page=1,
    per_page=PER_PAGE,
    query=None,
    status=None,
    order="slot",
    category=None,
    platform=None,
):
    """One row per catalogue entry, with `discs`, `out_count` and `slots`
    (comma-separated, in disc order) covering the whole set."""
    where, args = _where(query, status, category, platform)
    sql = (
        "SELECT disc.*,"
        " (SELECT COUNT(*) FROM disc m WHERE m.parent_id = disc.id) + 1 AS discs,"
        " (SELECT COUNT(*) FROM disc m WHERE (m.id = disc.id OR m.parent_id = disc.id)"
        "  AND m.status = '%s') AS out_count,"
        " (SELECT GROUP_CONCAT(slot, ', ') FROM (SELECT m.slot FROM disc m"
        "  WHERE m.id = disc.id OR m.parent_id = disc.id"
        "  ORDER BY m.disc_no, m.slot)) AS slots"
        " FROM disc WHERE 1=1" % OUT
    ) + where
    sql += " ORDER BY %s" % ("title COLLATE NOCASE" if order == "title" else "slot")
    sql += " LIMIT ? OFFSET ?"
    args += [per_page, (max(page, 1) - 1) * per_page]
    return conn.execute(sql, args).fetchall()


def all_discs(conn):
    """Every physical disc, sets included, by slot."""
    return conn.execute("SELECT * FROM disc ORDER BY slot").fetchall()


def get_disc(conn, disc_id):
    return conn.execute("SELECT * FROM disc WHERE id = ?", (disc_id,)).fetchone()


def lead_of(conn, disc):
    """The disc carrying the details for ``disc``: its lead, or itself."""
    if disc["parent_id"] is None:
        return disc
    return get_disc(conn, disc["parent_id"])


def set_discs(conn, lead_id):
    """The lead and its members in disc order, or [] for a disc on its own."""
    rows = conn.execute(
        "SELECT * FROM disc WHERE id = ? OR parent_id = ? ORDER BY disc_no, slot",
        (lead_id, lead_id),
    ).fetchall()
    return rows if len(rows) > 1 else []


def set_leads(conn, exclude_id):
    """Entries another disc could join: anything that is not itself a member."""
    return conn.execute(
        "SELECT * FROM disc WHERE parent_id IS NULL AND id != ?"
        " ORDER BY title COLLATE NOCASE",
        (exclude_id,),
    ).fetchall()


def has_members(conn, disc_id):
    return (
        conn.execute("SELECT 1 FROM disc WHERE parent_id = ?", (disc_id,)).fetchone()
        is not None
    )


def next_disc_no(conn, lead_id):
    row = conn.execute(
        "SELECT MAX(disc_no) FROM disc WHERE id = ? OR parent_id = ?",
        (lead_id, lead_id),
    ).fetchone()
    return (row[0] or 1) + 1


def disc_name(disc):
    if disc["disc_no"] is None:
        return disc["title"]
    return "%s, disc %d" % (disc["title"], disc["disc_no"])


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
        " ORDER BY e.at DESC, e.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def category_counts(conn):
    """How many entries in each category, for the filter bar. Unclassified
    discs are counted by nothing, so they appear only under "All"."""
    return {
        row["category"]: row["n"]
        for row in conn.execute(
            "SELECT category, COUNT(*) AS n FROM disc"
            " WHERE category IS NOT NULL AND parent_id IS NULL GROUP BY category"
        )
    }


def platform_counts(conn):
    """Same, per console, within the games. Drives the second filter row."""
    return {
        row["platform"]: row["n"]
        for row in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM disc"
            " WHERE category = ? AND platform IS NOT NULL AND parent_id IS NULL"
            " GROUP BY platform",
            (taxonomy.GAMES,),
        )
    }


def stats(conn):
    """Counts physical discs, not entries: they are what fill slots."""
    total = conn.execute("SELECT COUNT(*) FROM disc").fetchone()[0]
    out = conn.execute("SELECT COUNT(*) FROM disc WHERE status = ?", (OUT,)).fetchone()[
        0
    ]
    return {"total": total, "out": out, "free": SLOT_MAX - total}


# -- writes --------------------------------------------------------------


def log_event(conn, kind, disc_id=None, slot=None, detail=None):
    conn.execute(
        "INSERT INTO event (disc_id, slot, kind, detail, at) VALUES (?,?,?,?,?)",
        (disc_id, slot, kind, detail, _now()),
    )


def create_disc(
    conn,
    slot,
    title,
    subtitle=None,
    category=None,
    platform=None,
    notes=None,
    art_path=None,
    kind="added",
    parent_id=None,
):
    """Insert a disc. With ``parent_id`` it joins that set as its next disc,
    and the details other than the title belong to the lead."""
    now = _now()
    disc_no = None
    with conn:
        if parent_id is not None:
            lead = get_disc(conn, parent_id)
            title = lead["title"]
            subtitle = category = platform = notes = art_path = None
            disc_no = next_disc_no(conn, parent_id)
            conn.execute(
                "UPDATE disc SET disc_no = 1 WHERE id = ? AND disc_no IS NULL",
                (parent_id,),
            )
        cur = conn.execute(
            "INSERT INTO disc (slot, title, subtitle, category, platform, notes,"
            " art_path, status, parent_id, disc_no, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slot,
                title,
                subtitle,
                category,
                platform,
                notes,
                art_path,
                STORED,
                parent_id,
                disc_no,
                now,
                now,
            ),
        )
        log_event(conn, kind, cur.lastrowid, slot, title)
    return cur.lastrowid


def update_disc(conn, disc_id, **fields):
    allowed = (
        "title",
        "subtitle",
        "category",
        "platform",
        "notes",
        "art_path",
        "slot",
        "disc_no",
    )
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
        if "title" in fields:
            conn.execute(
                "UPDATE disc SET title = ? WHERE parent_id = ?",
                (fields["title"], disc_id),
            )


def join_set(conn, disc_id, lead_id):
    """Make ``disc_id`` the next disc of ``lead_id``'s set.

    The disc gives up its own details for the set's. Returns its old cover so
    the caller can remove the file.
    """
    disc = get_disc(conn, disc_id)
    lead = get_disc(conn, lead_id)
    with conn:
        conn.execute(
            "UPDATE disc SET disc_no = 1 WHERE id = ? AND disc_no IS NULL", (lead_id,)
        )
        conn.execute(
            "UPDATE disc SET parent_id = ?, disc_no = ?, title = ?, subtitle = NULL,"
            " category = NULL, platform = NULL, notes = NULL, art_path = NULL,"
            " updated_at = ? WHERE id = ?",
            (lead_id, next_disc_no(conn, lead_id), lead["title"], _now(), disc_id),
        )
        log_event(
            conn,
            "manual",
            disc_id,
            disc["slot"],
            "joined the set %s" % lead["title"],
        )
    return disc["art_path"]


def leave_set(conn, disc_id):
    disc = get_disc(conn, disc_id)
    with conn:
        conn.execute(
            "UPDATE disc SET parent_id = NULL, disc_no = NULL, updated_at = ?"
            " WHERE id = ?",
            (_now(), disc_id),
        )
        _tidy_set(conn, disc["parent_id"])
        log_event(conn, "manual", disc_id, disc["slot"], "left its set")


def _tidy_set(conn, lead_id):
    """A set down to one disc is just a disc again."""
    if not has_members(conn, lead_id):
        conn.execute("UPDATE disc SET disc_no = NULL WHERE id = ?", (lead_id,))


def mark_out(conn, disc_id, kind="ejected", detail=None):
    now = _now()
    with conn:
        conn.execute(
            "UPDATE disc SET status = ?, checked_out_at = ?, updated_at = ?"
            " WHERE id = ?",
            (OUT, now, now, disc_id),
        )
        row = get_disc(conn, disc_id)
        log_event(conn, kind, disc_id, row["slot"] if row else None, detail)


def mark_stored(conn, disc_id, kind="returned", detail=None):
    now = _now()
    with conn:
        conn.execute(
            "UPDATE disc SET status = ?, checked_out_at = NULL, updated_at = ?"
            " WHERE id = ?",
            (STORED, now, disc_id),
        )
        row = get_disc(conn, disc_id)
        log_event(conn, kind, disc_id, row["slot"] if row else None, detail)


def delete_disc(conn, disc_id):
    """Delete one disc. Returns ``(row, heir)``.

    Deleting a set's lead hands the set's details, cover included, to the next
    disc, which is returned as ``heir``; the caller must not remove the cover.
    """
    row = get_disc(conn, disc_id)
    heir = None
    with conn:
        if row is not None and row["parent_id"] is None:
            heir = conn.execute(
                "SELECT * FROM disc WHERE parent_id = ? ORDER BY disc_no, slot LIMIT 1",
                (disc_id,),
            ).fetchone()
        if heir is not None:
            conn.execute(
                "UPDATE disc SET parent_id = NULL, subtitle = ?, category = ?,"
                " platform = ?, notes = ?, art_path = ? WHERE id = ?",
                (
                    row["subtitle"],
                    row["category"],
                    row["platform"],
                    row["notes"],
                    row["art_path"],
                    heir["id"],
                ),
            )
            conn.execute(
                "UPDATE disc SET parent_id = ? WHERE parent_id = ?",
                (heir["id"], disc_id),
            )
            _tidy_set(conn, heir["id"])
        conn.execute("DELETE FROM disc WHERE id = ?", (disc_id,))
        if row is not None and row["parent_id"] is not None:
            _tidy_set(conn, row["parent_id"])
        log_event(
            conn,
            "removed",
            None,
            row["slot"] if row else None,
            disc_name(row) if row else None,
        )
    return row, heir
