"""
The JSON form of an impact report, pinned key by key and in key order.

The report covers every record report_data() writes: a table impact, a
finding, an unnamed lock, a lock, a window, a blocker, and a session.
"""

import json
import unittest

from sustained.analysis import MigrationStatement
from sustained.dialects import Dialects
from sustained.impact import (
    Blocks,
    EngineContext,
    TableStats,
    Work,
    analyze,
)
from sustained.impact.model import UnnamedLock
from sustained.impact.preflight import Blocker, LiveSession, Preflight
from sustained.impact.report import report_data


def pinned_report():
    context = EngineContext(
        "postgres",
        (16, 4),
        tables={"orders": TableStats(5_000_000, 900_000_000)},
        read=frozenset({"version", "sizes"}),
    )
    report = analyze(
        [
            MigrationStatement("CREATE INDEX ix ON orders (customer_id)", "m1", True),
            MigrationStatement(
                "ALTER TABLE orders ALTER COLUMN a TYPE bigint", "m1", True
            ),
            MigrationStatement("GRANT SELECT ON orders TO app", "m1", True),
        ],
        Dialects.POSTGRES,
        context,
    )
    migration = report.migrations[0]
    first = migration.statements[0]._replace(
        unnamed_locks=(UnnamedLock("SHARE", Blocks.WRITES, Work.SCAN),)
    )
    migration = migration._replace(statements=(first,) + migration.statements[1:])
    session = LiveSession(
        41, "pid 41", "app", "web", "idle in transaction", 75.5, "SELECT 1"
    )
    preflight = Preflight(
        "postgres",
        (
            Blocker(
                "ALTER TABLE orders",
                "orders",
                "ACCESS EXCLUSIVE",
                "ACCESS SHARE",
                True,
                session,
            ),
        ),
        (session,),
        60.0,
        frozenset({"locks", "sessions"}),
        frozenset({"locks"}),
        ("prepared transactions",),
    )
    return report._replace(migrations=(migration,), preflight=preflight)


PINNED = """
{
    "profile": "postgres",
    "version": "16.4",
    "evidence": "catalog",
    "read": [
        "sizes",
        "version"
    ],
    "migrations": [
        {
            "id": "m1",
            "transactional": true,
            "held_to_commit": true,
            "statements": [
                {
                    "sql": "CREATE INDEX ix ON orders (customer_id)",
                    "kind": "create_index",
                    "severity": "danger",
                    "confidence": "likely",
                    "evidence": "catalog",
                    "tables": [
                        {
                            "table": "orders",
                            "lock": "SHARE",
                            "blocks": "writes",
                            "work": "index_build",
                            "hold": "transaction",
                            "rows": 5000000,
                            "bytes": 900000000,
                            "rule": "pg.create_index"
                        }
                    ],
                    "findings": [
                        {
                            "rule": "pg.create_index",
                            "severity": "danger",
                            "message": "writes to orders wait for the whole index build; build it CONCURRENTLY in a migration with transactional=False",
                            "remedy": [
                                "CREATE INDEX CONCURRENTLY ix ON orders (customer_id)"
                            ],
                            "source": "https://www.postgresql.org/docs/current/sql-createindex.html"
                        },
                        {
                            "rule": "pg.partitions_unread",
                            "severity": "info",
                            "message": "the partitions were not read, so it is not known whether orders is a partitioned table or a partition; if orders is a partitioned table, each partition below it is also locked SHARE while the index is built on it, and the server refuses CREATE INDEX CONCURRENTLY on it",
                            "remedy": [],
                            "source": "https://www.postgresql.org/docs/current/ddl-partitioning.html"
                        },
                        {
                            "rule": "pg.lock_timeout",
                            "severity": "warn",
                            "message": "no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs",
                            "remedy": [
                                "SET LOCAL lock_timeout = '5s'"
                            ],
                            "source": "https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-LOCK-TIMEOUT"
                        }
                    ],
                    "partitions_unread": true,
                    "unnamed_locks": [
                        {
                            "lock": "SHARE",
                            "blocks": "writes",
                            "work": "scan"
                        }
                    ]
                },
                {
                    "sql": "ALTER TABLE orders ALTER COLUMN a TYPE bigint",
                    "kind": "alter_table",
                    "severity": "danger",
                    "confidence": "likely",
                    "evidence": "catalog",
                    "tables": [
                        {
                            "table": "orders",
                            "lock": "ACCESS EXCLUSIVE",
                            "blocks": "reads_and_writes",
                            "work": "rewrite",
                            "hold": "transaction",
                            "rows": 5000000,
                            "bytes": 900000000,
                            "rule": "pg.alter_column_type"
                        }
                    ],
                    "findings": [
                        {
                            "rule": "pg.alter_column_type",
                            "severity": "danger",
                            "message": "the column's current type is not known; a binary-coercible change, such as widening a varchar, would only change the catalog, so orders and its indexes are rewritten while reads and writes wait; the online route takes four steps: add a new column, write to both, backfill it, and swap it for a",
                            "remedy": [],
                            "source": "https://www.postgresql.org/docs/current/sql-altertable.html"
                        },
                        {
                            "rule": "pg.partitions_unread",
                            "severity": "info",
                            "message": "the partitions were not read, so it is not known whether orders is a partitioned table or a partition; if orders is a partitioned table, each partition below it is also locked ACCESS EXCLUSIVE",
                            "remedy": [],
                            "source": "https://www.postgresql.org/docs/current/ddl-partitioning.html"
                        },
                        {
                            "rule": "pg.lock_timeout",
                            "severity": "warn",
                            "message": "no lock_timeout in scope: while this statement waits for its lock, every query that conflicts with it on orders queues behind it, for as long as the longest open transaction runs",
                            "remedy": [
                                "SET LOCAL lock_timeout = '5s'"
                            ],
                            "source": "https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-LOCK-TIMEOUT"
                        }
                    ],
                    "partitions_unread": true,
                    "unnamed_locks": []
                },
                {
                    "sql": "GRANT SELECT ON orders TO app",
                    "kind": null,
                    "severity": "info",
                    "confidence": "unknown",
                    "evidence": "catalog",
                    "tables": [],
                    "findings": [
                        {
                            "rule": "impact.unknown",
                            "severity": "info",
                            "message": "the statement is not understood: no rule reads a GRANT statement",
                            "remedy": [],
                            "source": null
                        }
                    ],
                    "partitions_unread": false,
                    "unnamed_locks": []
                }
            ],
            "locks": [
                {
                    "table": "orders",
                    "lock": "SHARE",
                    "blocks": "writes",
                    "statement": 1
                },
                {
                    "table": "orders",
                    "lock": "ACCESS EXCLUSIVE",
                    "blocks": "reads_and_writes",
                    "statement": 2
                }
            ],
            "windows": [
                {
                    "table": "orders",
                    "blocks": "reads_and_writes",
                    "taken_by": 2,
                    "heaviest": "unknown",
                    "during": 3
                }
            ],
            "findings": [
                {
                    "rule": "window.held",
                    "severity": "warn",
                    "message": "orders stays blocked for writes from statement 1 and for reads_and_writes from statement 2 until the migration commits, across the unknown work of statement 3; move that work to a migration of its own",
                    "remedy": [],
                    "source": null
                }
            ]
        }
    ],
    "counts": {
        "info": 3,
        "warn": 3,
        "danger": 2
    },
    "preflight": {
        "profile": "postgres",
        "older_than": 60.0,
        "read": [
            "locks",
            "sessions"
        ],
        "needs": [
            "locks"
        ],
        "unread": [
            "prepared transactions"
        ],
        "blockers": [
            {
                "statement": "ALTER TABLE orders",
                "table": "orders",
                "lock": "ACCESS EXCLUSIVE",
                "held": "ACCESS SHARE",
                "granted": true,
                "session": {
                    "id": 41,
                    "label": "pid 41",
                    "user": "app",
                    "application": "web",
                    "state": "idle in transaction",
                    "transaction_seconds": 75.5,
                    "query": "SELECT 1"
                }
            }
        ],
        "transactions": [
            {
                "id": 41,
                "label": "pid 41",
                "user": "app",
                "application": "web",
                "state": "idle in transaction",
                "transaction_seconds": 75.5,
                "query": "SELECT 1"
            }
        ]
    }
}
"""


class ReportJsonTestCase(unittest.TestCase):
    def test_report_data_matches_the_pinned_output(self):
        self.maxDiff = None
        text = json.dumps(report_data(pinned_report()), indent=4)
        self.assertEqual(text, PINNED.strip())


if __name__ == "__main__":
    unittest.main()
