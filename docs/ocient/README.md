# CANOPY Users in Ocient

How a User is set up in production Ocient, and the design the Ocient query runner will follow.
The repo has no Ocient query runner yet.

**Nothing here has been run against a real Ocient system.** The SQL was written from the Ocient
docs (read 2026-11-25). Work through the [checklist](#checklist-for-a-real-cluster) before
relying on it.

## Set up a User

[`setup_user.sql`](setup_user.sql) is run once per User, by a DBA who may create users, schemas
and views in the Telemetry tables' database, and grant privileges on those schemas and tables.

| Parameter | Value |
|---|---|
| `<user>` | The CANOPY User name, as in `USERS`. The schema is `u_<user>`. |
| `<password>` | The new Ocient user's password. Ocient allows at most 32 characters and may require a mix (the `password_*` security settings). Do not use `'`. |
| `<telemetry_schema>` | The shared schema holding `flows`, `dns_metadata` and `diameter`. |

1. Make a copy outside the repo with the User name and schema filled in:

   ```
   sed -e 's/<user>/alice/g' -e 's/<telemetry_schema>/telemetry/g' \
       docs/ocient/setup_user.sql > ~/setup_alice.sql
   ```

2. In an editor, type the password into the copy, which keeps it out of shell history. For the
   pyocient CLI, also add `set stop_on_error on;` as the first line: without it the CLI carries on
   after a failed statement.
3. Run it on a connection to that database, for instance
   `pyocient "ocient://<dba>:<dba_password>@<host>:4050/<database>" -i ~/setup_alice.sql`.
   `CREATE USER` and `CREATE SCHEMA` fail if the name is taken: find out who made it before going
   on.
4. Delete the copy.

The User ends up with:

- view, create table, drop table and select on `u_<user>`
- select on the three shared Telemetry tables
- the views `u_<user>.flows`, `u_<user>.dns_metadata` and `u_<user>.diameter`
- no database-level SELECT or CREATE TABLE

Ocient gives the creator of a table every privilege on it, so the User can also insert into,
rename and delete the tables they create.

**A new Telemetry table** in the Semantic Layer needs its `GRANT` and `CREATE VIEW` added to the
script and run for each existing User. **A changed column list** may need the view replaced
(`CREATE OR REPLACE VIEW`).

**To remove a User**, deleting their tables: `DROP SCHEMA u_<user> CASCADE;` then
`DROP USER "<user>";`.

## Design notes for the Ocient query runner

- **One Ocient user per User.** Its credentials go in `.env`, never in `defaults.toml` or git.
  The settings that hold them are added with the runner.
- **One pyocient connection per User**, never shared between Users.
- **`SET SCHEMA u_<user>` right after connecting.** Ocient has no search path, so bare names
  (`flows`, a Result Table's name) resolve through the connection's default schema.
  - Send it without a trailing semicolon: pyocient parses this command itself and takes the last
    word as the schema name.
  - pyocient 3.5.0 or later restores the schema when it reconnects. If the restore fails, it only
    logs the error and carries on, so the runner should confirm `SELECT CURRENT_SCHEMA()` rather
    than assume it.
- **One query at a time on a connection.** Executing a new query on a pyocient connection cancels
  the one still running there, so the runner must queue a User's queries.
- **The User's tables live in `u_<user>`**: their `rt_*` Result Tables, Saved Tables and Lookup
  Tables. Creating and replacing Result Tables is covered in
  [`ocient-table-replacement.md`](../research/ocient-table-replacement.md).

## Checklist for a real cluster

Set up two Users, `alice` and `bob`. "As alice" means a new connection with alice's credentials,
followed by `SET SCHEMA u_alice;` unless the step says otherwise. Do them in this order: item 5
uses a table made in item 2.

### 1. Partition pruning on `etime` works through the views

As alice, compare the two plans for a narrow time range, then repeat for `dns_metadata` and
`diameter`:

```sql
EXPLAIN SELECT COUNT(*) FROM flows WHERE etime >= <start> AND etime < <end>;
EXPLAIN SELECT COUNT(*) FROM <telemetry_schema>.flows WHERE etime >= <start> AND etime < <end>;
```

Pass: both plans apply the `etime` range at the scan of the base table, with the same estimated
rows and cost. Run both queries too: their run times should be alike, and both far quicker than
without the time range. If naming the base table is refused, run the second as the DBA.

### 2. Whether CTAS into `u_<user>` needs database-level CREATE TABLE

The docs disagree: the CTAS reference asks for `CREATE TABLE` on the database, the schema
reference for `CREATE TABLE` on an explicit schema. The script grants only the latter. As alice,
and again as bob with `u_bob`:

```sql
CREATE TABLE u_alice.rt_check AS (SELECT * FROM flows LIMIT 10);
CREATE TABLE rt_check_bare AS (SELECT * FROM flows LIMIT 10);
INSERT INTO rt_check_bare SELECT * FROM flows LIMIT 1;
ALTER TABLE rt_check_bare RENAME TO rt_check_renamed;
DROP TABLE rt_check_renamed;
```

Pass: all succeed, and both tables were created in `u_alice`. Keep `rt_check` for item 5. If a
privilege error asks for the database-level grant, do not add it: record the error and revisit
the design.

### 3. The default schema a new session starts in

As alice, without `SET SCHEMA`, then after it:

```sql
SELECT CURRENT_SCHEMA();
SET SCHEMA u_alice;
SELECT CURRENT_SCHEMA();
```

Record the first answer (the docs say it is the fully qualified user name, so not `u_alice`).
Also run `SELECT COUNT(*) FROM flows` before `SET SCHEMA`: it should fail. The last answer must
name `u_alice`. Then break the connection (for instance, restart the SQL Node), run
`SELECT CURRENT_SCHEMA()` again on the same pyocient connection, and check it still does.

### 4. An unqualified name not in `u_<user>` fails

As the DBA, make a table alice may read but that is not in her schema:

```sql
CREATE TABLE <telemetry_schema>.canopy_check AS (SELECT * FROM sys.dummy1);
GRANT SELECT ON TABLE <telemetry_schema>.canopy_check TO USER "alice";
```

As alice, `SELECT * FROM canopy_check;` must fail with an unknown table, while
`SELECT * FROM <telemetry_schema>.canopy_check;` shows the grant is in place (if that is refused,
alice also needs `VIEW` on the shared schema for this test). Repeat with the table in any other
schema alice can see. Drop the test tables afterwards.

### 5. One User can't read or list another User's schema

As bob, each of these must fail:

```sql
SELECT * FROM u_alice.rt_check;
SELECT * FROM u_alice.flows;
CREATE TABLE u_alice.rt_intruder AS (SELECT * FROM flows LIMIT 1);
DROP TABLE u_alice.rt_check;
```

And each of these must return no rows:

```sql
SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = 'u_alice';
SELECT name FROM sys.views WHERE "schema" = 'u_alice';
SELECT name FROM sys.schemas WHERE name = 'u_alice';
```

As the DBA, check that the schemas are explicit (`is_implicit` is false for both) and review
what each User holds. There must be no database-level SELECT or CREATE TABLE, whether granted to
the User, to a group or role of theirs, or to `PUBLIC`:

```sql
SELECT name, is_implicit FROM sys.schemas WHERE name IN ('u_alice', 'u_bob');
SELECT grantee, privilege, privilege_target, object_type FROM sys.privileges;
```

Ocient's Public role may create schemas by default. A User can't read anything that way, but
could take the name `u_<user>` before that User is set up, which stops the setup script.

Drop `u_alice.rt_check` and `u_bob.rt_check` when done.

## Sources

[DCL (GRANT)](https://docs.ocient.com/data-control-language-dcl-statement-reference.md),
[users](https://docs.ocient.com/users-groups-and-service-classes.md),
[schemas](https://docs.ocient.com/schemas.md),
[views](https://docs.ocient.com/views.md),
[tables and CTAS](https://docs.ocient.com/tables.md),
[default schema](https://docs.ocient.com/data-query-language-dql-statement-reference.md),
[SET SCHEMA](https://docs.ocient.com/commands-supported-by-the-ocient-jdbc-cli-program.md),
[identifiers](https://docs.ocient.com/identifiers.md),
[password settings](https://docs.ocient.com/database-password-security-settings.md),
[EXPLAIN](https://docs.ocient.com/query-analysis.md),
[`CURRENT_SCHEMA`](https://docs.ocient.com/other-functions-and-expressions.md),
[system catalog](https://docs.ocient.com/system-catalog.md),
[pyocient](https://docs.ocient.com/connect-using-pyocient.md).
The notes on how pyocient handles `SET SCHEMA` come from reading the pyocient 3.10.1 source.
