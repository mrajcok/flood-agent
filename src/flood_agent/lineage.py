"""Column lineage: which output columns of one query carry values from a Restricted Column (ADR 0008).

`analyse` reads the SQL, never the database. It parses it with sqlglot (the postgres dialect, for SQLite and Ocient
alike), works out which tables it reads, and follows each output column back through expressions, set operations,
CTEs and subqueries to the columns it takes its values from. It fails closed: whatever it cannot be sure of is
`Unknown`, never "nothing restricted".
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema

log = logging.getLogger("uvicorn.error")

DIALECT = "postgres"
_OWN_DATABASES = ("main", "results")  # the SQLite names of the Telemetry Database and the User's own tables
_UNNAMED = "_col_"  # how qualification names an output column that has no name of its own
_DUPLICATE_SUFFIX = re.compile(r":\d+$")  # how SQLite tells apart two output columns of one name: `imsi:1`


@dataclass(frozen=True)
class TableSchema:
    """A table a query may read: its column names, and which of them are Restricted Columns."""

    columns: tuple[str, ...]
    restricted: frozenset[str] = frozenset()


SchemaProvider = Callable[[str], TableSchema | None]
"""The schema of the table of this name (any case), or None if the User has no such table to read."""


@dataclass(frozen=True)
class Known:
    """Lineage was worked out. `restricted` names the restricted output columns, in output order; `sources` is every
    (table, column) the query reads, lower-cased, whatever it reads it for."""

    restricted: tuple[str, ...] = ()
    sources: frozenset[tuple[str, str]] = field(default=frozenset(), compare=False)


@dataclass(frozen=True)
class Unknown:
    """Lineage could not be worked out."""


@dataclass(frozen=True)
class Unrestricted:
    """No table the query reads has a Restricted Column, so lineage was not needed."""


Outcome = Known | Unknown | Unrestricted


def restricted_columns(outcome: Outcome) -> list[str] | None:
    """The restricted output columns the Table Catalog records for this outcome; None if lineage is unknown."""
    match outcome:
        case Known():
            return list(outcome.restricted)
        case Unrestricted():
            return []
        case Unknown():
            return None


class _Unresolved(Exception):
    """Something in the query that lineage cannot be sure of. The message says what, without any value."""


def analyse(sql: str, schema: SchemaProvider, result_columns: list[str]) -> Outcome:
    """The lineage of `sql`, a query as the User wrote it, whose output columns are named `result_columns` by the
    database that ran it. Never raises."""
    try:
        query = _parse(sql)
        tables = {}
        for name in _tables_read(query):
            table = schema(name)
            if table is None:
                raise _Unresolved("a table that is not known")
            tables[name] = table
        if not any(t.restricted for t in tables.values()):
            return Unrestricted()
        return _Lineage(query, tables).known(result_columns)
    except Exception as e:  # sqlglot raises many kinds of error, and every one of them means the same here
        log.info("Column lineage unknown: %s", type(e).__name__)  # never the message: it can quote the SQL
        return Unknown()


def _parse(sql: str) -> exp.Query:
    statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise _Unresolved("not a single query")
    query = statements[0]
    for identifier in query.find_all(exp.Identifier):  # SQLite ignores case, quoted or not
        identifier.set("this", identifier.name.lower())
    return query


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
    for test in query.find_all(exp.In):  # SQLite's `x IN table_name`
        operand = test.args.get("field")
        if operand is not None:
            names.add(operand.name)
    return names


def _bare_word(node: exp.Expr) -> str | None:
    """The word `node` was written as, if sqlglot takes it for something other than a column (`current_user`)
    although SQLite reads a column of that name where there is one."""
    if isinstance(node, exp.Boolean) or isinstance(node, exp.Func) and not any(node.args.values()):
        return node.sql(DIALECT).lower()
    return node.name.lower() if isinstance(node, exp.Var) else None


class _Lineage:
    """One qualified query over known tables."""

    def __init__(self, query: exp.Query, tables: dict[str, TableSchema]):
        self._drop_database_names(query)
        self._restricted = {name: {c.lower() for c in t.restricted} for name, t in tables.items()}
        self._columns = {name: [c.lower() for c in t.columns] for name, t in tables.items()}  # in table order
        mapping: dict[str, object] = {name: dict.fromkeys(columns, "TEXT") for name, columns in self._columns.items()}
        self._query = qualify(
            query, dialect=DIALECT, schema=MappingSchema(mapping, dialect=DIALECT, normalize=False),
            validate_qualify_columns=True, quote_identifiers=False, identify=False,
        )
        scopes = traverse_scope(self._query)
        self._scopes = {id(scope.expression): scope for scope in scopes}
        self._outputs: dict[int, list[tuple[str, bool]]] = {}
        self._check(scopes)

    @staticmethod
    def _drop_database_names(query: exp.Query) -> None:
        for table in query.find_all(exp.Table):
            if table.db:
                if table.name in _cte_names(table):
                    raise _Unresolved("a table and a CTE of one name")
                table.set("db", None)
        for column in query.find_all(exp.Column):
            column.set("db", None)

    def _check(self, scopes: list[Scope]) -> None:
        # [name] quotes an identifier in SQLite, but parses here as an array of whatever is inside it.
        if self._query.find(exp.Array, exp.Bracket):
            raise _Unresolved("square brackets")
        restricted = set().union(*self._restricted.values())
        if any(_bare_word(node) in restricted for node in self._query.walk()):
            raise _Unresolved("a word that SQLite may read as a restricted column")
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

    def known(self, result_columns: list[str]) -> Known:
        outputs = self._scope_outputs(self._scopes[id(self._query)])
        if len(outputs) != len(result_columns):
            raise _Unresolved("a different number of output columns from the database's")
        for projection, actual in zip(self._query.selects, result_columns):
            name = _carried_name(projection)
            if name and name not in (actual.lower(), _DUPLICATE_SUFFIX.sub("", actual.lower())):
                raise _Unresolved("output columns in a different order from the database's")
        restricted = tuple(actual for (_, carries), actual in zip(outputs, result_columns) if carries)
        return Known(restricted, self._sources())

    def _sources(self) -> frozenset[tuple[str, str]]:
        """Every (table, column) read anywhere in the query, checking that each column resolves."""
        sources: set[tuple[str, str]] = set()
        for column in self._query.find_all(exp.Column):
            scope = self._scope_of(column)
            if not column.table:
                self._check_output_reference(column, scope)
                continue
            source = self._source(column, scope)
            if isinstance(source, exp.Table):
                if column.name not in self._columns[source.name]:
                    raise _Unresolved("a column its table does not have")
                sources.add((source.name, column.name))
        return frozenset(sources)

    def _scope_of(self, node: exp.Expr) -> Scope:
        parent = node.parent
        while parent is not None:
            if id(parent) in self._scopes:
                return self._scopes[id(parent)]
            parent = parent.parent
        raise _Unresolved("a column outside any query")

    @staticmethod
    def _check_output_reference(column: exp.Column, scope: Scope) -> None:
        """A column left without a table may only be an ORDER BY naming an output column."""
        outputs = cast(exp.Query, scope.expression).named_selects
        if column.find_ancestor(exp.Order) is None or column.name not in outputs:
            raise _Unresolved("a column that does not resolve")

    @staticmethod
    def _source(column: exp.Column, scope: Scope | None) -> exp.Table | Scope:
        """What `column`'s qualifier names, looking outwards from `scope` (a correlated subquery)."""
        while scope is not None:
            if column.table in scope.selected_sources:
                return scope.selected_sources[column.table][1]
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
            case exp.WithinGroup():  # here the ORDER BY holds the values aggregated
                ordered = node.expression.expressions
                return any(self._carries(n, scope) for n in [node.this, *(o.this for o in ordered)])
            case exp.Select() | exp.SetOperation():
                return any(carries for _, carries in self._scope_outputs(self._scopes[id(node)]))
        return any(self._carries(child, scope) for child in node.iter_expressions())

    def _column_carries(self, column: exp.Column, scope: Scope) -> bool:
        source = self._source(column, scope)
        if isinstance(source, exp.Table):
            return column.name in self._restricted[source.name]
        matches = [carries for name, carries in self._scope_outputs(source) if name == column.name]
        if not matches:
            raise _Unresolved("a column its subquery does not have")
        return any(matches)


def _carried_name(projection: exp.Expr) -> str | None:
    """The name of an output column that takes it from the column it selects (as each column of an expanded `*`
    does), which the database must agree on; None for any other output column."""
    if not isinstance(projection, exp.Alias) or projection.alias.startswith(_UNNAMED):
        return None
    selected = projection.this
    columns = selected.expressions + [selected.this] if isinstance(selected, exp.Coalesce) else [selected]
    names = {c.name if isinstance(c, exp.Column) else None for c in columns}
    return projection.alias if names == {projection.alias} else None
