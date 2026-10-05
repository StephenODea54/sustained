"""SQL Server: the full lifecycle. Rehearsal is off the allowlist, so it
runs on a scratch database."""

from sustained.dialects import Dialects

from . import impact_mssql, lifecycle, queries, round_trip, transactions, writes


class MssqlLifecycle(lifecycle.ServerCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MssqlQueries(queries.QueriesCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL


class MssqlWrites(writes.WritesCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    HAS_RETURNING = False
    HAS_CTAS = False


class MssqlTransactions(transactions.TransactionsCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL


class MssqlImpact(impact_mssql.MssqlImpactCase):
    NAME = "mssql"


class MssqlRoundTrip(round_trip.RoundTripCase):
    NAME = "mssql"
    DIALECT = Dialects.MSSQL
    EXPECTED = {
        "ix_rt_plain": ((False,), (None,), False),
        "ix_rt_desc": ((True, False), (None, None), False),
        "ux_rt_b": ((False, True), (None, None), False),
        "ix_rt_part": ((False,), (None,), True),
    }
