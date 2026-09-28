"""The correction layer each gap's lesson landed in, appended and never updated."""
import re
import time

from .enums import GapLayer
from .schema import _transaction

TICKET_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+\Z")


def record_gap_layer(conn, ticket_id, layer, note, author, carried_by=None,
                     now=None):
    layers = [member.value for member in GapLayer]
    if layer not in layers:
        raise ValueError(f"layer {layer!r} is not one of {', '.join(layers)}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("note is blank")
    if not isinstance(author, str) or not author.strip():
        raise ValueError("author is blank")
    if carried_by is not None and not TICKET_IDENTIFIER.match(carried_by):
        raise ValueError(f"carried_by {carried_by!r} is not a ticket"
                         " identifier such as KO-7")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM tickets WHERE id = ?",
                        (ticket_id,)).fetchone() is None:
            raise ValueError(f"ticket {ticket_id!r} is not in the store")
        cursor = conn.execute(
            "INSERT INTO gapLayers (ticketId, layer, note, carriedBy, author,"
            " at) VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, layer, note, carried_by, author, now))
    return cursor.lastrowid


def gap_layer_counts(conn):
    counts = dict.fromkeys((member.value for member in GapLayer), 0)
    for layer, count in conn.execute(
            "SELECT layer, COUNT(*) FROM gapLayers WHERE id IN"
            " (SELECT MAX(id) FROM gapLayers GROUP BY ticketId)"
            " GROUP BY layer"):
        counts[layer] = count
    return counts
