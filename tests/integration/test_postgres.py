"""PostgreSQL: the full lifecycle, rehearsed in place."""

from sustained.dialects import Dialects

from . import (
    aio_lifecycle,
    impact,
    lifecycle,
    queries,
    round_trip,
    transactions,
    writes,
)


class PostgresLifecycle(lifecycle.ServerCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
    REHEARSES_IN_PLACE = True
    HAS_ADVISORY_LOCK = True


class PostgresQueries(queries.QueriesCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresWrites(writes.WritesCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresTransactions(transactions.TransactionsCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresAsync(aio_lifecycle.AsyncCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresImpact(impact.ImpactCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES


class PostgresRoundTrip(round_trip.RoundTripCase):
    NAME = "postgres"
    DIALECT = Dialects.POSTGRES
    EXPECTED = {
        "ix_rt_plain": ((False,), (None,), False),
        "ix_rt_desc": ((True, False), (None, None), False),
        "ux_rt_b": ((False, True), (None, None), False),
        "ix_rt_part": ((False,), (None,), True),
    }
