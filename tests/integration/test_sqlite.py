"""SQLite: the default dialect, in process, with no advisory lock."""

from sustained.dialects import Dialects

from . import (
    aio_lifecycle,
    impact_sqlite,
    lifecycle,
    queries,
    round_trip,
    transactions,
    writes,
)


class SqliteLifecycle(lifecycle.ServerCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = False


class SqliteQueries(queries.QueriesCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteWrites(writes.WritesCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteTransactions(transactions.TransactionsCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteAsync(aio_lifecycle.AsyncCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT


class SqliteImpact(impact_sqlite.SqliteImpactCase):
    NAME = "sqlite"


class SqliteRoundTrip(round_trip.RoundTripCase):
    NAME = "sqlite"
    DIALECT = Dialects.DEFAULT
    EXPECTED = {
        "ix_rt_plain": ((False,), (None,), False),
        "ix_rt_desc": ((True, False), (None, None), False),
        "ux_rt_b": ((False, True), (None, None), False),
        "ix_rt_part": ((False,), (None,), True),
    }
