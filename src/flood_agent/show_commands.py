"""SHOW / DESCRIBE TABLE for a SQL Cell's Command drop-down.

These are not queries: they make no table, have no row cap, and their rows are kept with the Run. On the
SQLite development database SHOW TABLES, SHOW COLUMNS and DESCRIBE TABLE are emulated from the tables a query
may read; the other 13 commands are not available there.
"""

from pathlib import Path

from flood_agent import queries

SHOW_COMMANDS = (
    "SHOW TABLES", "SHOW VIEWS", "SHOW COLUMNS", "SHOW INDEXES", "SHOW SCHEMATA", "SHOW DATABASES", "SHOW USERS",
    "SHOW GROUPS", "SHOW NODES", "SHOW PIPELINES", "SHOW PIPELINE_STATUS", "SHOW TABLE_PRIVILEGES",
    "SHOW GEOMETRY_COLUMNS", "SHOW DATA_TYPES", "SHOW RESERVED_WORDS", "SHOW SYSTEM TABLES", "DESCRIBE TABLE",
)
DESCRIBE_TABLE = "DESCRIBE TABLE"
NOT_AVAILABLE_ON_DEV = "{command} is not available on the development database."
SHOW_IN_TEXTBOX = "SHOW and DESCRIBE are not run from the SQL box: pick one in the Command drop-down instead."


class ShowFailed(Exception):
    """The command cannot run; the message is plain and fit for the user."""


def is_show_statement(sql: str) -> bool:
    """True if `sql` starts with SHOW or DESCRIBE (comments aside): it belongs in the Command drop-down."""
    words, _ = queries._scan(sql)
    return bool(words) and words[0] in ("show", "describe", "desc")


def statement_of(command: str, table: str) -> str:
    """The statement a Command (and, for DESCRIBE TABLE, its table) stands for."""
    return f"{command} {table.strip()}" if command == DESCRIBE_TABLE else command


def run_show(command: str, table: str, telemetry_db: Path, results_db: Path,
             listing: dict[str, list[str]]) -> tuple[list[str], list[list]]:
    """The emulated command's (column names, rows). `listing` is the readable tables by kind (see
    `TableCatalog.listing`): only those are shown or described. ShowFailed for a command with no emulation or a
    table that is not readable."""
    kinds = {n: k for k in ("telemetry", "lookup", "saved", "result") for n in listing.get(k, [])}

    def columns_of(name: str) -> list[dict]:
        return queries.table_columns(telemetry_db, results_db, name, kinds[name] == "telemetry") or []

    if command == "SHOW TABLES":
        return ["table_name", "table_type"], [[n, kinds[n]] for n in sorted(kinds)]
    if command == "SHOW COLUMNS":
        return (["table_name", "column_name", "ordinal_position", "data_type"],
                [[n, c["name"], i, c["type"]] for n in sorted(kinds) for i, c in enumerate(columns_of(n), 1)])
    if command == DESCRIBE_TABLE:
        name = next((n for n in kinds if n.lower() == table.strip().lower()), None)
        if name is None:
            raise ShowFailed(f"{table.strip() or 'No table'} is not a table you can describe.")
        return (["column_name", "data_type", "ordinal_position"],
                [[c["name"], c["type"], i] for i, c in enumerate(columns_of(name), 1)])
    raise ShowFailed(NOT_AVAILABLE_ON_DEV.format(command=command))
