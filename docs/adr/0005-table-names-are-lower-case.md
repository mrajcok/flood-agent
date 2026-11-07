# Table names are kept in lower case

SQL table names ignore case. The code used to compare them inconsistently: some checks lower-cased the name, and some (`show_table`, `show_chart`, the registry lookup) matched it exactly. As a result, a Saved Table renamed to `MyTable` could be queried as `mytable` but not shown under that name. We now lower-case every table name when it is created or renamed, the same way Lookup Tables already were, so every comparison after that can be exact. The Table Catalog is the only place that applies this rule.

## Considered Options

- Keep names exactly as the user typed them, and compare them case-insensitively everywhere through one helper. We rejected this because it keeps the user's capitalisation in the UI only by making every comparison remember the rule, and those 24 places that each had to remember it are where the bugs came from.

## Consequences

- When a user renames a table to `MyTable`, it is shown and queried as `mytable`.
