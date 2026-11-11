import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import config
from policy_search import PolicySearch

# The agent's tools. Two design rules worth noticing:
#
# 1. Every tool is scoped to the logged-in customer. The model never passes a
#    customer id, so it cannot read or change someone else's booking, however
#    it is prompted. Security lives in code, not in the system prompt.
# 2. Business rules (refund amounts, fees, eligibility) are calculated here,
#    not by the model. Small models are bad at arithmetic and rules; the model's
#    job is to pick the right tool and explain the result.

ADMIN_FEE = 10.00
DELAY_REPAY_BANDS = [(120, 1.00), (60, 1.00), (30, 0.50), (15, 0.25)]  # (min delay, share of price)


class ToolError(Exception):
    """A problem the model should see and explain (e.g. booking not found)."""


class Tools:
    def __init__(self, customer_email):
        self.db = sqlite3.connect(config.DB_PATH)
        self.db.row_factory = sqlite3.Row
        customer = self.db.execute("SELECT * FROM customers WHERE email = ?", (customer_email,)).fetchone()
        if customer is None:
            raise SystemExit(f"No customer with email {customer_email}. Did you run seed_db.py?")
        self.customer = dict(customer)
        self.policies = PolicySearch()

    # ---------- helpers ----------

    def _now(self):
        return datetime.now()

    def _booking(self, booking_id):
        row = self.db.execute("""
            SELECT b.*, j.origin, j.dest, j.depart_at, j.arrive_at, j.operator, j.status AS train_status,
                   j.delay_mins, so.name AS origin_name, sd.name AS dest_name
            FROM bookings b
            JOIN journeys j ON j.id = b.journey_id
            JOIN stations so ON so.code = j.origin
            JOIN stations sd ON sd.code = j.dest
            WHERE b.id = ? AND b.customer_id = ?""",
            (str(booking_id).upper().strip(), self.customer["id"])).fetchone()
        if row is None:
            raise ToolError(f"No booking {booking_id} found on this customer's account.")
        return dict(row)

    def _describe_time(self, iso):
        when = datetime.fromisoformat(iso)
        days = (when.date() - self._now().date()).days
        relative = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(
            days, f"in {days} days" if days > 0 else f"{-days} days ago")
        return f"{when:%A %d %b, %H:%M} ({relative})"

    def _describe_status(self, b):
        if b["train_status"] == "cancelled":
            return "CANCELLED by the operator"
        if b["train_status"] == "delayed":
            return f"DELAYED by {b['delay_mins']} minutes"
        return "on time"

    # ---------- read-only tools ----------

    def get_my_bookings(self):
        rows = self.db.execute(
            "SELECT b.id FROM bookings b JOIN journeys j ON j.id = b.journey_id "
            "WHERE b.customer_id = ? ORDER BY j.depart_at", (self.customer["id"],)).fetchall()
        # Readable fields ("Wednesday 14 Oct, 09:00 (in 2 days)", "CANCELLED") help small
        # models a lot. Given only raw ISO dates, llama3.1 often matched the wrong booking.
        return [{
            "booking_id": b["id"],
            "route": f"{b['origin_name']} to {b['dest_name']}",
            "departs": self._describe_time(b["depart_at"]),
            "train": self._describe_status(b),
            "fare": f"{b['fare_type']}, £{b['price']:.2f}",
            "seat": b["seat"],
            "booking_status": b["status"],
        } for b in (self._booking(r["id"]) for r in rows)]

    def search_policies(self, query):
        return [{"source": h["source"], "text": h["text"]} for h in self.policies.search(query)]

    # ---------- write tools ----------
    # Each takes dry_run. The agent calls it with dry_run=True first to get a
    # preview, asks the human to confirm, and only then runs it for real.

    def request_refund(self, booking_id, dry_run=True):
        b = self._booking(booking_id)
        now = self._now()
        depart = datetime.fromisoformat(b["depart_at"])
        if b["status"] == "refunded":
            raise ToolError(f"Booking {b['id']} has already been refunded.")

        if b["train_status"] == "cancelled":
            kind, amount = "cancelled by operator (full refund, no fee)", b["price"]
        elif depart < now:
            # Already travelled: only Delay Repay is possible
            if now - depart > timedelta(days=28):
                raise ToolError("Delay Repay must be claimed within 28 days of travel.")
            if self.db.execute("SELECT 1 FROM refunds WHERE booking_id = ? AND reason LIKE 'delay repay%'",
                               (b["id"],)).fetchone():
                raise ToolError(f"A Delay Repay claim already exists for booking {b['id']}.")
            share = next((s for mins, s in DELAY_REPAY_BANDS if b["delay_mins"] >= mins), 0)
            if share == 0:
                raise ToolError(f"The train was {b['delay_mins']} minutes late. Delay Repay needs 15+ minutes. "
                                "Tickets cannot be refunded after departure otherwise.")
            kind, amount = f"delay repay ({b['delay_mins']} min late, {int(share * 100)}% of fare)", b["price"] * share
        else:
            if b["fare_type"] == "Advance":
                raise ToolError("Advance tickets are non-refundable unless the train is cancelled or delayed by 15+ minutes.")
            kind, amount = f"customer cancellation ({b['fare_type']}, minus £{ADMIN_FEE:.2f} admin fee)", b["price"] - ADMIN_FEE

        amount = round(amount, 2)
        summary = {"booking_id": b["id"], "route": f"{b['origin_name']} to {b['dest_name']}",
                   "refund_type": kind, "refund_amount": f"£{amount:.2f}"}
        if dry_run:
            return summary
        self.db.execute("INSERT INTO refunds (booking_id, amount, reason, status, created_at) VALUES (?, ?, ?, 'approved', ?)",
                        (b["id"], amount, kind, now.isoformat(timespec="minutes")))
        if not kind.startswith("delay repay"):
            self.db.execute("UPDATE bookings SET status = 'refunded' WHERE id = ?", (b["id"],))
        self.db.commit()
        return {**summary, "result": "Refund approved. Paid to the original card within 5 working days."}

    # ---------- text-to-SQL tool ----------
    # Unlike the tools above, the model writes the query itself. That makes it flexible
    # ("trains around 12 on the 15th", "fastest train to York") but model-written SQL
    # must never be trusted, so it runs on a separate connection that:
    #   - is opened read-only (mode=ro), so INSERT/UPDATE/DELETE/DROP all fail
    #   - has an authorizer that only allows reading the timetable tables,
    #     so customers, bookings and refunds stay private

    def search_trains_sql(self, sql):
        ro = sqlite3.connect(Path(config.DB_PATH).as_uri() + "?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        ro.set_authorizer(_timetable_only)
        try:
            rows = ro.execute(sql).fetchmany(config.MAX_SQL_ROWS)
        except sqlite3.Error as e:
            # Send the database error back so the model can fix its query and try again
            raise ToolError(f"SQL error: {e}. Only SELECT on the journeys and stations tables is allowed.")
        finally:
            ro.close()
        if not rows:
            return {"rows": [], "note": "No trains matched. Check station names, the date and the time range."}
        return {"rows": [dict(r) for r in rows]}

    def escalate_to_human(self, summary):
        cur = self.db.execute("INSERT INTO tickets (customer_id, summary, created_at) VALUES (?, ?, ?)",
                              (self.customer["id"], summary, self._now().isoformat(timespec="minutes")))
        self.db.commit()
        return {"ticket_id": cur.lastrowid, "result": "A human agent will reply by email within 24 hours."}


def _timetable_only(action, table, column, db_name, trigger):
    """SQLite calls this for every table/column a query touches. Deny anything else."""
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if table in ("journeys", "stations") else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


# Tools that change data. The agent asks the human before running these.
WRITE_TOOLS = {"request_refund"}

# What the model sees: a name, a description and a JSON schema for each tool.
# The descriptions matter a lot. They are the model's only guide to when to use a tool.
TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_my_bookings",
        "description": "List all of the customer's bookings with booking ids, routes, departure times, fares "
                       "and live train status (on time / delayed with minutes / cancelled).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "search_policies",
        "description": "Search the official policies on refunds, Delay Repay compensation and railcards. "
                       "Use this before answering any question about rules, fees or entitlements.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look up, e.g. 'refund for a late train'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "request_refund",
        "description": "Refund a booking or claim Delay Repay. The system works out eligibility and the amount. "
                       "The customer is asked to confirm before it is submitted.",
        "parameters": {"type": "object", "properties": {
            "booking_id": {"type": "string", "description": "Booking id, e.g. BK1001"}},
            "required": ["booking_id"]}}},
    {"type": "function", "function": {
        "name": "search_trains_sql",
        # For text-to-SQL the description IS the database documentation: the model
        # can only write a correct query if it knows the tables, columns and formats.
        "description": """Find trains in the timetable by writing ONE SQLite SELECT query. Use this when the customer wants to find or compare trains.
Tables:
  stations(code TEXT, name TEXT), e.g. ('SHF', 'Sheffield'), ('KGX', 'London King's Cross')
  journeys(id INTEGER, origin TEXT, dest TEXT, depart_at TEXT, arrive_at TEXT, operator TEXT, status TEXT, delay_mins INTEGER)
    origin and dest are station codes. depart_at/arrive_at look like '2026-10-15T12:30'.
    status is 'on time', 'delayed' or 'cancelled'.
Match places by joining stations and using LIKE on the name, because a city can have several stations.
Use date(depart_at) for the day and time(depart_at) for the time of day.
Example (York to Edinburgh, morning of 2026-10-20):
  SELECT j.depart_at, j.arrive_at, o.name AS origin, d.name AS destination, j.operator, j.status, j.delay_mins
  FROM journeys j JOIN stations o ON o.code = j.origin JOIN stations d ON d.code = j.dest
  WHERE o.name LIKE '%York%' AND d.name LIKE '%Edinburgh%'
    AND date(j.depart_at) = '2026-10-20' AND time(j.depart_at) BETWEEN '06:00' AND '12:00'
  ORDER BY j.depart_at""",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "A single SQLite SELECT statement"}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human agent. Use when the customer is very upset, asks for a human, "
                       "or wants something the other tools cannot do (e.g. changing a booking).",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "Short summary of the issue for the human agent"}},
            "required": ["summary"]}}},
]
