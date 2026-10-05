"""MySQL: the full lifecycle. Schema changes do not roll back, so the
rehearsal runs on a scratch database."""

from sustained.dialects import Dialects

from . import impact_innodb, lifecycle, queries, round_trip, transactions, writes


class MysqlLifecycle(lifecycle.ServerCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MysqlQueries(queries.QueriesCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL


class MysqlWrites(writes.WritesCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
    HAS_RETURNING = False


class MysqlTransactions(transactions.TransactionsCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL


class MysqlImpact(impact_innodb.InnodbImpactCase):
    NAME = "mysql"
    PROFILE = "mysql"


class MysqlRoundTrip(round_trip.RoundTripCase):
    NAME = "mysql"
    DIALECT = Dialects.MYSQL
    EXPECTED = {
        "ix_rt_plain": ((False,), (None,), False),
        "ix_rt_desc": ((True, False), (None, None), False),
        "ux_rt_b": ((False, True), (None, None), False),
        "ix_rt_prefix": ((False, True), (10, None), False),
    }
