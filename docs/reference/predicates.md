---
layout: default
title: SQL predicates and expressions reference
description: "Reference for sustained.expressions and sustained.functions: column predicates, literals, raw SQL, and the per-dialect function registry."
---

Everything in `sustained.expressions`, plus the function registry in `sustained.functions`. These objects keep columns, literals, and conditions apart from one another, so the builder never has to guess which one a string was meant to be.

Guide: [Filtering](/filtering).

## Typed columns

`col(name)` returns a `ColumnExpr`. `Model.c.<column>` returns one too, and also checks the name against the model's declared columns.

```python
from sustained import col

col('venues.capacity') > 1400
Venue.c.capacity > 1400          # the same predicate, with a typo check
```

### `ColumnExpr`

The `name` attribute is the column path as you wrote it.

Comparison operators return a `Predicate`. A `ColumnExpr` on the right side of a comparison renders as a column, not as a bound value.

| Operator | Renders | Notes |
| --- | --- | --- |
| `==` | `=` | `== None` renders `IS NULL`. |
| `!=` | `!=` | `!= None` renders `IS NOT NULL`. |
| `>` `>=` `<` `<=` | the same operator | Comparing to `None` raises `ValueError`. |

Each method below returns a `Predicate`.

| Method | Renders |
| --- | --- |
| `like(pattern)` | `LIKE` |
| `not_like(pattern)` | `NOT LIKE` |
| `ilike(pattern)` | `ILIKE`, native on Postgres and DuckDB, `LOWER() LIKE LOWER()` elsewhere |
| `in_(values)` | `IN (...)` over a list or a `QueryBuilder`. An empty list or a string raises `ValueError`, so `in_('active')` does not match each character. |
| `not_in(values)` | `NOT IN (...)`. An empty list raises `ValueError`. |
| `between(low, high)` | `BETWEEN low AND high` |
| `not_between(low, high)` | `NOT BETWEEN low AND high` |
| `is_null()` | `IS NULL` |
| `not_null()` | `IS NOT NULL` |

### `Predicate`

A composable condition. Pass a `Predicate` to `where()` or `having()` as the only argument.

| Operator | Renders |
| --- | --- |
| `a & b` | `(a AND b)` |
| <code>a &#124; b</code> | `(a OR b)` |
| `~a` | `NOT (a)` |

`bool(predicate)` always raises `TypeError`, so `a and b` fails instead of evaluating to one side of the expression. Use `&` and `|`.

## Columns, values, and raw SQL

Every argument you pass to the builder is a column, a value, or raw SQL. Sustained quotes a column for the active dialect, binds a value as a parameter (or writes it as an escaped literal where the position renders literals), and writes raw SQL as it is.

A plain string takes its meaning from its position. In a column position it is a column: `select()`, the column of `where()` and `having()`, `orderBy()`, `groupBy()`, aggregates, function arguments, window partitions and orders, both sides of a join `ON`, and the keys of `insert()` and `update()`. In a value position it is a value: the value of `where()`, the members of `IN` and `BETWEEN`, the values of `insert()` and `update()`, and `CASE` results.

A column string follows one rule everywhere. It is `*`, `table.*`, a call on one column such as `'COUNT(id)'`, or a dotted path. The function name of a call goes into the SQL as given, so a column string must not come from untrusted input. Each part of a path takes the dialect's quotes, and a part already in `".."`, `[..]` or `` `..` `` quotes loses those quotes first. The keys of `insert()` and `update()` are one name each, so `'a.b'` there names one column called `a.b`.

If you want to override the position, wrap the argument. Each wrapper means the same thing in every position.

```python
col(name)
```
{: .sig #col-wrapper}

The argument is a column. It follows the column string rule, also in a value position, so `where('a', '=', col('b'))` compares two columns.

```python
Literal(value)
```
{: .sig #literal}

The argument is a value, even in a column position. In a function argument, a select list, or a `CASE` result it renders as an inline literal. In `where()`, `insert()`, and `update()` it binds as a parameter.

`orderBy()` and the `groupBy()` family raise `ValueError` for a `Literal`. In `ORDER BY` and `GROUP BY` the number `1` names the first column of the select list and not the value `1`. To sort or group by a position, write `raw('1')`.

```python
from sustained import Literal

query.select_func('COALESCE', 'nickname', 'name', Literal('unknown'), alias='display')

# COALESCE("nickname", "name", 'unknown') AS "display"   (on Postgres)
```

```python
raw(sql)
```
{: .sig #raw}

The argument is raw SQL. Sustained writes the text as it is, without quotes or parameters, so never build it from a request. `raw()` is in the `sustained` package, and `QueryBuilder.raw()` returns the same object.

```python
Column(sql)
```
{: .sig #column}

`Column` is a deprecated name for `raw()`. It works in every position that takes `raw()` and raises a `DeprecationWarning` when you create one. It will be removed in 3.0.

A literal can be a string, a number, a boolean, `None`, a `Decimal`, a `date`, a `datetime`, or `bytes`. A date or timestamp renders as a typed literal such as `DATE '2024-05-17'`, and a `datetime` with a time zone renders as `TIMESTAMPTZ` on Postgres and DuckDB. On the default dialect a date renders as its ISO text, because SQLite stores dates as text. MSSQL casts the ISO text to `DATE`, `DATETIME2`, or `DATETIMEOFFSET`. `bytes` renders as `X'...'`, as `decode('...', 'hex')` on Postgres, as `from_hex('...')` on DuckDB, and as `0x...` on MSSQL. A `Decimal` that is not finite raises `ValueError`. `str(query)` and CASE results render their values the same way.

A function argument string that is not a column name is quoted as one, so a forgotten `Literal()` makes the database report an unknown column. The default dialect writes names without quotes, so it raises `ValueError` for such a string.

`Expression(value)`, in `sustained.types` and re-exported from `sustained.schema`, does the same job for schema defaults: raw SQL that renders as written in both the inline and the parameterized forms.

## Expression objects

The fluent methods on `QueryBuilder` build these objects for you. Construct one directly when you need a form the fluent method does not cover.

```python
Func(function_name, *args, alias=None)
```
{: .sig #func}

A function call, the object `select_func()` builds.

```python
AggregateExpression(function_name, column, alias=None)
```
{: .sig #aggregateexpression}

An aggregate, the object `count()` and its siblings build.

```python
WindowExpression(function_name, alias, partition_by=None, order_by=None, args=None, frame=None)
```
{: .sig #windowexpression}

A window function, the object `select_window()` builds.

```python
CaseExpression(alias, else_result)
```
{: .sig #caseexpression}

A `CASE` expression. `when(condition, result)` appends a `WHEN`/`THEN` pair and returns the `CaseExpression`, so pairs chain. `whens` returns a copy of the pairs.

```python
Subquery(query, alias)
```
{: .sig #subquery}

Embeds a `QueryBuilder` in a SELECT list or a join:

```python
from sustained.expressions import Subquery

ticket_count = (Ticket.query()
    .count()
    .where('show_id', '=', col('shows.id'))
)

Show.query().select('title', Subquery(ticket_count, 'tickets_sold'))
```

`render(ctx)` renders the subquery with the outer statement's render context, so its values parameterize with the rest of the statement. `str()` inlines them as literals, for reading and logging.

`render_operand(ctx)` renders it with no alias, for the places where the subquery stands as a value or a column, such as a function argument, one side of a comparison, or the column of `where()`, `orderBy()`, `groupBy()`, an aggregate, a window, or a join `ON`. The compiler calls it with the statement's context in those places, so `to_sql()` binds the subquery's values as parameters. Passing `None` for the context inlines the values.

### Aliases in a nested position

An alias belongs to the select list, so Sustained leaves it off where one of these objects stands as a value (a function argument, or the value side of a comparison). `Func`, `AggregateExpression`, `WindowExpression`, `CaseExpression`, and `Subquery` all drop it there, so you can pass the same object to `select()` and to a function call and get valid SQL from both.

A nested object also renders through the compiler of the statement that contains it, not through the default dialect. A `CASE` with boolean results renders `1` and `0` on MS SQL Server and `TRUE` and `FALSE` elsewhere, in the select list and inside a function call alike.

## Function registry

`select_func()` and the fluent function methods check the name against `FunctionRegistry` in `sustained.functions`. A registered function that the active dialect cannot spell raises `DialectError` while the query builds. An unregistered name passes through unchecked, so you can call a function the registry does not list.

| Function | Available on | Per-dialect spelling |
| --- | --- | --- |
| `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` | every dialect | one spelling |
| `LOWER`, `UPPER`, `COALESCE`, `CONCAT`, `SUBSTRING`, `TRIM`, `ROUND`, `ABS`, `CEILING`, `FLOOR`, `MOD` | every dialect | one spelling |
| `LENGTH` | every dialect | `LEN` on MSSQL |
| `STRING_AGG` | Postgres, DuckDB, Presto, Athena | one spelling |
| `NOW` | Postgres, MySQL, DuckDB, Presto, Athena | `GETDATE` on MSSQL |
| `GETDATE` | MSSQL | `NOW` on Postgres, MySQL, DuckDB, Presto, and Athena |

Write either `NOW()` or `GETDATE()` and the dialect renders its own spelling. Neither one is registered for the default dialect, so both raise `DialectError` there.

`STRING_AGG` is left off MySQL on purpose. MySQL spells the same idea as `GROUP_CONCAT`, which takes its separator as a `SEPARATOR` keyword rather than a second argument, so a renamed function would produce SQL that does not parse. Write `GROUP_CONCAT` through raw SQL there.

### Registry API

```python
FunctionRegistry.register(name, metadata)
```
{: .sig #register}

Registers or overwrites an entry. The key is the uppercased name.

```python
FunctionRegistry.get_metadata(name) -> FunctionMetadata
```
{: .sig #get_metadata}

Case-insensitive lookup. Raises `KeyError` when the name is unregistered.

```python
FunctionRegistry.resolve_name(name, dialect) -> str
```
{: .sig #resolve_name}

The dialect's spelling, or the name uppercased.

```python
FunctionRegistry.is_supported(name, dialect) -> bool
```
{: .sig #is_supported}

`True` for any unregistered name.

`FunctionMetadata(supported_dialects, dialect_names={})` is a `NamedTuple`. Register your own metadata to get build-time checking for a function the registry does not list:

```python
from sustained.dialects import Dialects
from sustained.functions import FunctionMetadata, FunctionRegistry

FunctionRegistry.register(
    'DATE_TRUNC',
    FunctionMetadata(supported_dialects=[Dialects.POSTGRES, Dialects.DUCKDB]),
)
```

## Type aliases

These live in `sustained.types`. Use them to annotate code that accepts what the builder accepts.

| Alias | Definition |
| --- | --- |
| `DbReturnValue` | <code>str &#124; int &#124; float &#124; bool &#124; datetime &#124; date &#124; Decimal &#124; bytes</code> |
| `Selectable` | Anything `select()` takes |
| `CaseCondition` | <code>str &#124; Predicate</code>: a `CASE` condition, where a string is raw SQL |
| `CaseResult` | <code>DbReturnValue &#124; Expression &#124; ColumnExpr &#124; Literal &#124; Func</code> |
| `ColumnReference` | <code>str &#124; Expression &#124; ColumnExpr &#124; Literal &#124; Func</code>: the column of `where()`, `having()`, `orderBy()`, `groupBy()`, and a join `ON`. `orderBy()` and `groupBy()` raise `ValueError` for a `Literal` |
| `QueryResolvable` | <code>QueryBuilder &#124; Callable[..., QueryBuilder] &#124; Expression</code> |
| `Join` | <code>BasicJoinMapping &#124; JoinMappingWithThrough</code> |

The relation-mapping types are `TypedDict`s: `RelationMapping`, `BasicJoinMapping`, `JoinMappingWithThrough`, `ThroughJoinMapping`, and `ThroughJoinValue`. See [Model](/reference/model#relations).
