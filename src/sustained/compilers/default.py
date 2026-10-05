from .base import Compiler


class DefaultCompiler(Compiler):
    """
    Compiler for the default dialect, which runs against SQLite. Queries
    write identifiers bare, as the base compiler does. DDL quotes every
    identifier with double quotes, which SQLite and ANSI SQL both take,
    so a table or column named after a keyword such as order still
    creates, rebuilds, and drops.
    """

    # SQLite, which the default dialect renders for, accepts a partial index.
    supports_partial_index = True
    _DDL_IDENT_QUOTES = ('"', '"')
