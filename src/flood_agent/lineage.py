"""Column lineage: which output columns of one query carry values from a Restricted Column (ADR 0008).

`analyse` is given the SQL and what SQLite itself says compiling it reads (`Reads`). Whether any Restricted Column
is within the query's reach comes from SQLite alone. Which output columns its values reach comes from parsing the
query, rewritten so the parser reads it as SQLite does (`sqlite_sql`), with sqlglot (the postgres dialect), following
each output column back through expressions, set operations, CTEs and subqueries to the columns it takes its values
from. Only query shapes whose names both are sure to resolve alike are followed (`_check_shape`, and the `_check_*`
methods of `_Lineage`). It fails closed: whatever it cannot be sure of is `Unknown`, never "nothing restricted".
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

from flood_agent import sqlite_sql
from flood_agent.sqlite_sql import fold

log = logging.getLogger("uvicorn.error")

DIALECT = "postgres"
_OWN_DATABASES = ("main", "results")  # the SQLite names of the Telemetry Database and the User's own tables
_UNNAMED = "_col_"  # how an output column is marked that has no name of its own
_DUPLICATE_SUFFIX = re.compile(r":\d+$")  # how SQLite tells apart two output columns of one name: `imsi:1`


@dataclass(frozen=True)
class TableSchema:
    """A table a query may read: its column names in table order, and which of them are Restricted Columns."""

    columns: tuple[str, ...]
    restricted: frozenset[str] = frozenset()


SchemaProvider = Callable[[str], TableSchema | None]
"""The schema of the table of this name (any case), or None if the User has no such table to read."""


@dataclass(frozen=True)
class Reads:
    """What SQLite says compiling a query reads. Names are folded as SQLite compares them (`sqlite_sql.fold`)."""

    # Every table it opens or reads a column of, with the table's columns as the database has them, in order; empty
    # for something read that is not a table of either database (sqlite_master, a table function).
    tables: Mapping[str, tuple[str, ...]]
    columns: frozenset[tuple[str, str]]  # the (table, column) pairs its authorizer reports; it misses some


@dataclass(frozen=True)
class Known:
    """Lineage was worked out. `restricted` names the restricted output columns, in output order; `sources` is every
    (table, column) the query reads, folded, whatever it reads it for."""

    restricted: tuple[str, ...] = ()
    sources: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)


@dataclass(frozen=True)
class Unknown:
    """Lineage could not be worked out."""


@dataclass(frozen=True)
class Unrestricted:
    """No table the query reads has a Restricted Column, so lineage was not needed."""


Outcome = Known | Unknown | Unrestricted


@dataclass(frozen=True)
class Restriction:
    """Which columns of a table carry restricted values: `columns`, or None if that is unknown."""

    columns: tuple[str, ...] | None = ()

    @property
    def unknown(self) -> bool:
        return self.columns is None

    @property
    def restricted(self) -> bool:
        """Whether the table has a Restricted Column, or may have one."""
        return self.columns is None or bool(self.columns)


UNKNOWN = Restriction(None)


def restriction(outcome: Outcome) -> Restriction:
    """The restriction of a table made by a query with this lineage."""
    match outcome:
        case Known():
            return Restriction(outcome.restricted)
        case Unrestricted():
            return Restriction()
        case Unknown():
            return UNKNOWN


class _Unresolved(Exception):
    """Something in the query that lineage cannot be sure of. The message says what, without any value."""


def analyse(sql: str, schema: SchemaProvider, result_columns: list[str], reads: Reads) -> Outcome:
    """The lineage of `sql`, a query as the User wrote it, whose output columns are named `result_columns` by the
    database that ran it and which reads `reads`. Never raises."""
    try:
        read = {name: _known_table(schema, name) for name in reads.tables}
        if not any(t.restricted for t in read.values()):
            return Unrestricted()
        for name, table in read.items():
            # Otherwise the schema describes other rows than these, or `*` would not expand in their order.
            if tuple(map(fold, table.columns)) != reads.tables[name]:
                raise _Unresolved("a table whose columns are not as its schema says")
            for column in table.columns:
                sqlite_sql.check_name(column)
        query = _parse(sqlite_sql.canonical(sql, (c for t in read.values() for c in t.columns)))
        listed = _tables_read(query)
        _check_shape(query)
        if not read.keys() <= listed:
            raise _Unresolved("a table SQLite reads that the parser does not see")
        tables = read | {name: _known_table(schema, name) for name in listed - read.keys()}
        known = _Lineage(query, tables).known(result_columns)
        missed = {(t, c) for t, c in reads.columns if t in tables and c in map(fold, tables[t].restricted)}
        if not missed <= known.sources:
            raise _Unresolved("a restricted column SQLite reads that the parser does not see")
        return known
    except Exception as e:  # sqlglot raises many kinds of error, and every one of them means the same here
        log.info("Column lineage unknown: %s", type(e).__name__)  # never the message: it can quote the SQL
        return Unknown()


def _known_table(schema: SchemaProvider, name: str) -> TableSchema:
    table = schema(name)
    if table is None:
        raise _Unresolved("a table that is not known")
    return table


def _parse(sql: str) -> exp.Query:
    statements = sqlglot.parse(sql, dialect=DIALECT)
    query = statements[0] if len(statements) == 1 else None
    if not isinstance(query, exp.Query):
        raise _Unresolved("not a single query")
    for identifier in query.find_all(exp.Identifier):  # SQLite ignores case, quoted or not
        identifier.set("this", fold(identifier.name))
    _mark_unnamed(query)
    return query


def _mark_unnamed(query: exp.Query) -> None:
    """Give every selected expression that has no alias a made-up one, so that the parser derives no name from the
    expression itself: the name SQLite gives it may differ."""
    unnamed = [p for select in query.find_all(exp.Select) for p in select.expressions
               if not isinstance(p, exp.Alias | exp.Column | exp.Star)]
    for number, projection in enumerate(unnamed):
        alias = exp.Alias(alias=exp.to_identifier(f"{_UNNAMED}u{number}"))
        projection.replace(alias)
        alias.set("this", projection)


def _check_shape(query: exp.Query) -> None:
    """That the query, before it is qualified, has none of the shapes SQLite and the parser may read differently."""
    for column in query.find_all(exp.Column):
        if column.args.get("db") or column.args.get("catalog"):
            raise _Unresolved("a column qualified with its database")
    for join in query.find_all(exp.Join):
        if join.method == "NATURAL" or join.args.get("using") and join.side in ("RIGHT", "FULL"):
            raise _Unresolved("a NATURAL join, or a RIGHT or FULL join with USING")
    if any(test.args.get("field") for test in query.find_all(exp.In)):
        raise _Unresolved("a table named after IN")  # which the parser would take for a column
    for clause in query.find_all(exp.Limit, exp.Offset, exp.WindowSpec):  # they say how many rows are returned or counted
        if clause.find(exp.Select):
            raise _Unresolved("a subquery in LIMIT, OFFSET or a window frame")
    _check_one_meaning_per_name(query)


def _check_one_meaning_per_name(query: exp.Query) -> None:
    """That every name of a table, CTE or source alias stands for one thing wherever it is written. Which of two things
    of one name a qualifier means depends on rules of scope in which the two differ; with one meaning it cannot matter."""
    meanings: dict[str, set[tuple[str, object]]] = {}

    def means(name: str, kind: str, which: object) -> None:
        meanings.setdefault(name, set()).add((kind, which))

    for table in query.find_all(exp.Table):
        kind = "cte" if not table.db and table.name in _cte_names(table) else "table"
        means(table.name, kind, table.name)
        means(table.alias_or_name, kind, table.name)
    for cte in query.find_all(exp.CTE):
        means(cte.alias_or_name, "cte", cte.alias_or_name)
    for subquery in query.find_all(exp.Subquery):
        if subquery.alias:
            if not isinstance(subquery.this, exp.Select | exp.SetOperation):
                raise _Unresolved("an alias for something in brackets that is not a query")
            means(subquery.alias, "subquery", id(subquery))
    if any(len(found) > 1 for found in meanings.values()):
        raise _Unresolved("one name for two things")


@dataclass(frozen=True)
class _BareColumn:
    """A column written without a qualifier: its name, the query it is written in, and whether it may be one of that
    query's output names (in a clause that only filters, groups or orders)."""

    name: str
    query: exp.Expr
    own_alias: bool


def _bare_columns(query: exp.Query) -> list[_BareColumn]:
    """The columns written without a qualifier. To be taken before the query is qualified."""
    bare = []
    for column in query.find_all(exp.Column):
        if column.table:
            continue
        child, parent = column, column.parent
        while parent is not None and not isinstance(parent, exp.Select | exp.SetOperation):
            child, parent = parent, parent.parent
        if parent is None:
            raise _Unresolved("a column outside any query")
        named = column.name in cast(exp.Query, parent).named_selects
        bare.append(_BareColumn(column.name, parent, named and child.arg_key in ("where", "group", "having", "order")))
    return bare


def _inner_queries(query: exp.Query) -> list[exp.Expr]:
    """The queries of the CTEs and of the subqueries in FROM and JOIN: those whose columns the query around them reads
    by name."""
    inner = [cte.this for cte in query.find_all(exp.CTE)]
    for subquery in query.find_all(exp.Subquery):
        if subquery.arg_key == "this" and isinstance(subquery.parent, exp.From | exp.Join):
            body = subquery.unnest()
            if isinstance(body, exp.Select | exp.SetOperation):
                inner.append(body)
    return inner


def _cte_names(node: exp.Expr) -> set[str]:
    """The names of the CTEs of every WITH clause around `node`."""
    names: set[str] = set()
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Query):
            names |= {cte.alias_or_name for cte in parent.ctes}
        parent = parent.parent
    return names


def _tables_read(query: exp.Query) -> set[str]:
    """The names of the tables the query reads: every table named that is not one of its CTEs."""
    names: set[str] = set()
    for table in query.find_all(exp.Table):
        if not table.name:
            raise _Unresolved("a table function")
        if table.catalog or table.db and table.db not in _OWN_DATABASES:
            raise _Unresolved("another database")
        if table.db or table.name not in _cte_names(table):
            names.add(table.name)
    return names


class _Lineage:
    """One qualified query over known tables."""

    def __init__(self, query: exp.Query, tables: dict[str, TableSchema]):
        if any(join.args.get("using") for join in query.find_all(exp.Join)):
            # The parser puts a USING column first in `*`, and SQLite leaves it in place.
            if any(not isinstance(star.parent, exp.Count) for star in query.find_all(exp.Star)):
                raise _Unresolved("a * over a join with USING")
        bare = _bare_columns(query)
        column_lists = [(cte.this, cte.args["alias"].columns) for cte in query.find_all(exp.CTE)]
        column_lists = [(body, [c.name for c in names]) for body, names in column_lists if names]
        for table in query.find_all(exp.Table):
            table.set("db", None)  # each name here means one thing (`_check_shape`)
        self._restricted = {name: {fold(c) for c in t.restricted} for name, t in tables.items()}
        self._columns = {name: [fold(c) for c in t.columns] for name, t in tables.items()}  # in table order
        mapping: dict[str, object] = {name: dict.fromkeys(columns, "TEXT") for name, columns in self._columns.items()}
        # SQLite lets no selected expression refer to another's alias, so none is expanded; `_sources` checks that
        # what is left without a table only filters, groups or orders.
        self._query = qualify(
            query, dialect=DIALECT, schema=MappingSchema(mapping, dialect=DIALECT, normalize=False),
            expand_alias_refs=False, validate_qualify_columns=False, quote_identifiers=False, identify=False,
        )
        scopes = traverse_scope(self._query)
        self._scopes = {id(scope.expression): scope for scope in scopes}
        self._outputs: dict[int, list[tuple[str, bool]]] = {}
        self._check(scopes)
        self._check_inner_names(column_lists)
        self._check_bare_columns(bare)

    def _check(self, scopes: list[Scope]) -> None:
        ctes = [cte.alias_or_name for cte in self._query.find_all(exp.CTE)]
        # In a CTE's body SQLite reads a later CTE of the same WITH, where the parser reads an outer one of that name.
        if len(set(ctes)) != len(ctes):
            raise _Unresolved("two CTEs of one name")
        for cte in self._query.find_all(exp.CTE):
            # Restricted values can move between columns on each pass, which following the query once would miss.
            if any(not t.db and t.name == cte.alias_or_name for t in cte.this.find_all(exp.Table)):
                raise _Unresolved("a recursive query")
        for star in self._query.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise _Unresolved("a * left unexpanded")
        for scope in scopes:
            if not isinstance(scope.expression, exp.Select | exp.SetOperation):
                raise _Unresolved("an unsupported construct")
            for _, source in scope.selected_sources.values():
                # SQLite may see a CTE here that sqlglot takes for a table: one named before it is defined.
                if isinstance(source, exp.Table) and source.name in _cte_names(source):
                    raise _Unresolved("a CTE used before it is defined")

    def _check_inner_names(self, column_lists: list[tuple[exp.Expr, list[str]]]) -> None:
        """That each column of a subquery or CTE has the same name in SQLite as here. SQLite makes up a name for a
        column that has none of its own (its text), for the second of two of one name (`name:1`) and for one called
        true or false; a reference by such a name would be taken elsewhere here."""
        for inner in _inner_queries(self._query):
            names = [name for name, _ in self._scope_outputs(self._scopes[id(inner)])]
            if len(set(names)) != len(names) or any(
                    name.startswith(_UNNAMED) or ":" in name or name in ("true", "false") for name in names):
                raise _Unresolved("a subquery column without a plain name of its own")
        for body, listed in column_lists:
            if [name for name, _ in self._scope_outputs(self._scopes[id(body)])] != listed:
                raise _Unresolved("a CTE's column list that is not followed")

    def _check_bare_columns(self, bare: list[_BareColumn]) -> None:
        """That each column written without a qualifier is one of the sources of its own query (or, where allowed, one
        of its output names), which both look at first. Further out the two search in different ways."""
        for column in bare:
            scope = self._scopes[id(column.query)]
            if not column.own_alias and not any(column.name in self._names_of(source)
                                                for _, source in scope.selected_sources.values()):
                raise _Unresolved("a column without a qualifier that its own query does not have")

    def known(self, result_columns: list[str]) -> Known:
        outputs = self._scope_outputs(self._scopes[id(self._query)])
        if len(outputs) != len(result_columns):
            raise _Unresolved("a different number of output columns from the database's")
        for projection, actual in zip(self._query.selects, result_columns):
            name = _carried_name(projection)
            if name and name not in (fold(actual), _DUPLICATE_SUFFIX.sub("", fold(actual))):
                raise _Unresolved("output columns in a different order from the database's")
        restricted = tuple(actual for (_, carries), actual in zip(outputs, result_columns) if carries)
        return Known(restricted, self._sources())

    def _sources(self) -> frozenset[tuple[str, str]]:
        """Every (table, column) read anywhere in the query, checking that each column resolves."""
        sources: set[tuple[str, str]] = set()
        for column in self._query.find_all(exp.Column):
            scope = self._scope_of(column)
            if not column.table:
                sources |= self._filter_reads(column, scope)
                continue
            source = self._source(column, scope)
            if column.name not in self._names_of(source):
                raise _Unresolved("a column its source does not have")
            if isinstance(source, exp.Table):
                sources.add((source.name, column.name))
        return frozenset(sources)

    def _scope_of(self, node: exp.Expr) -> Scope:
        parent = node.parent
        while parent is not None:
            if id(parent) in self._scopes:
                return self._scopes[id(parent)]
            parent = parent.parent
        raise _Unresolved("a column outside any query")

    def _filter_reads(self, column: exp.Column, scope: Scope) -> set[tuple[str, str]]:
        """What a column left without a table reads. It may only be where it cannot select a value (WHERE, GROUP BY,
        HAVING, ORDER BY), naming an output column or a column of a source."""
        clause = column.find_ancestor(exp.Where, exp.Group, exp.Having, exp.Order, exp.Query)
        if clause is None or isinstance(clause, exp.Query):
            raise _Unresolved("a column that does not resolve")
        if column.name in cast(exp.Query, scope.expression).named_selects and isinstance(clause, exp.Order):
            return set()
        return {(source.name, column.name) for _, source in scope.selected_sources.values()
                if isinstance(source, exp.Table) and column.name in self._names_of(source)}

    def _names_of(self, source: exp.Table | Scope) -> list[str]:
        """The column names of a table or a subquery."""
        if isinstance(source, exp.Table):
            return self._columns[source.name]
        return [name for name, _ in self._scope_outputs(source)]

    @staticmethod
    def _source(column: exp.Column, scope: Scope | None) -> exp.Table | Scope:
        """What `column`'s qualifier names, looking outwards from `scope` (a correlated subquery, which a subquery in
        FROM or a CTE may not be)."""
        while scope is not None:
            if column.table in scope.selected_sources:
                return scope.selected_sources[column.table][1]
            if _walled(scope.expression):
                break
            scope = scope.parent
        raise _Unresolved("a column that does not resolve")

    def _scope_outputs(self, scope: Scope) -> list[tuple[str, bool]]:
        """(name, whether it carries restricted values) for each output column of a query."""
        key = id(scope)
        if key in self._outputs:
            return self._outputs[key]
        if isinstance(scope.expression, exp.SetOperation):
            left, right = (self._scope_outputs(side) for side in scope.set_operation_scopes)
            if len(left) != len(right):
                raise _Unresolved("a set operation whose sides differ")
            outputs = [(name, carries or other) for (name, carries), (_, other) in zip(left, right)]
        else:
            selects = cast(exp.Query, scope.expression).selects
            outputs = [(p.alias_or_name, self._carries(p, scope)) for p in selects]
        self._outputs[key] = outputs
        return outputs

    def _carries(self, node: exp.Expr, scope: Scope) -> bool:
        """Whether a restricted value reaches the value of `node`, an expression in `scope`."""
        match node:
            case exp.Count():
                return False
            case exp.Column():
                return self._column_carries(node, scope)
            case exp.Window() | exp.Filter() | exp.Order():  # PARTITION BY, ORDER BY and FILTER only choose rows
                return self._carries(node.this, scope)
            case exp.Select() | exp.SetOperation():
                return any(carries for _, carries in self._scope_outputs(self._scopes[id(node)]))
        return any(self._carries(child, scope) for child in node.iter_expressions())

    def _column_carries(self, column: exp.Column, scope: Scope) -> bool:
        if not column.table:
            raise _Unresolved("a selected column that does not resolve")
        source = self._source(column, scope)
        if isinstance(source, exp.Table):
            return column.name in self._restricted[source.name]
        matches = [carries for name, carries in self._scope_outputs(source) if name == column.name]
        if not matches:
            raise _Unresolved("a column its subquery does not have")
        return any(matches)


def _walled(query: exp.Expr) -> bool:
    """Whether `query` is a CTE's, or a subquery in FROM or JOIN: one that SQLite resolves apart from the sources
    around it."""
    while isinstance(query.parent, exp.Subquery):
        query = query.parent
    return isinstance(query.parent, exp.CTE | exp.From | exp.Join) and query.arg_key == "this"


def _carried_name(projection: exp.Expr) -> str | None:
    """The name of an output column that takes it from the column it selects (as each column of an expanded `*`
    does), which the database must agree on; None for any other output column."""
    if not isinstance(projection, exp.Alias) or projection.alias.startswith(_UNNAMED):
        return None
    selected = projection.this
    columns = selected.expressions + [selected.this] if isinstance(selected, exp.Coalesce) else [selected]
    names = {c.name if isinstance(c, exp.Column) else None for c in columns}
    return projection.alias if names == {projection.alias} else None
