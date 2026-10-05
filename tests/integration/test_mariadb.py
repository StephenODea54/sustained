"""MariaDB: the same dialect as MySQL against a different server."""

from sustained.dialects import Dialects

from . import impact_innodb, lifecycle, queries, round_trip, transactions, writes


class MariadbLifecycle(lifecycle.ServerCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    REHEARSES_IN_PLACE = False
    HAS_ADVISORY_LOCK = True


class MariadbQueries(queries.QueriesCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL


class MariadbWrites(writes.WritesCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    HAS_RETURNING = False


class MariadbTransactions(transactions.TransactionsCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL


class MariadbImpact(impact_innodb.InnodbImpactCase):
    NAME = "mariadb"
    PROFILE = "mariadb"


class MariadbRoundTrip(round_trip.RoundTripCase):
    NAME = "mariadb"
    DIALECT = Dialects.MYSQL
    EXPECTED = {
        "ix_rt_plain": ((False,), (None,), False),
        "ix_rt_desc": ((True, False), (None, None), False),
        "ux_rt_b": ((False, True), (None, None), False),
        "ix_rt_prefix": ((False, True), (10, None), False),
    }
