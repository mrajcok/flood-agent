-- Sets up one CANOPY User in production Ocient. A DBA runs it once per User.
-- Read README.md next to this file first: how to run it, and what to check on a real cluster.
-- Written from the Ocient docs and not yet run against a real Ocient system.
--
-- Parameters, written in angle brackets below. Replace every occurrence in a copy of this file:
--   user              the CANOPY User name ([a-z][a-z0-9_]{0,30})
--   password          the password of the new Ocient user
--   telemetry_schema  the shared schema that holds the Telemetry tables
--
-- Connect to the database that holds the Telemetry tables: the Ocient user is created in the
-- database of the connection. Stop at the first error.
--
-- Never add SELECT or CREATE TABLE ON DATABASE: one User could then read another User's schema,
-- or create tables outside their own.

-- 1. The Ocient user.
CREATE USER "<user>" PASSWORD = '<password>';

-- 2. Their schema, created explicitly: Ocient grants Public VIEW on a schema it creates
--    implicitly. No IF NOT EXISTS, so a schema that somebody else made stops the script.
CREATE SCHEMA u_<user>;

-- 3. Their own schema: see it, create and drop tables in it, and select from it.
GRANT VIEW ON SCHEMA u_<user> TO USER "<user>";
GRANT VIEW TABLE ON SCHEMA u_<user> TO USER "<user>";
GRANT VIEW VIEW ON SCHEMA u_<user> TO USER "<user>";
GRANT CREATE TABLE ON SCHEMA u_<user> TO USER "<user>";
GRANT DROP TABLE ON SCHEMA u_<user> TO USER "<user>";
GRANT SELECT ON SCHEMA u_<user> TO USER "<user>";

-- 4. The shared Telemetry tables the views read, and nothing else in that schema.
--    One line here and one view below for each table in semantic_layer.yaml.
GRANT SELECT ON TABLE <telemetry_schema>.flows TO USER "<user>";
GRANT SELECT ON TABLE <telemetry_schema>.dns_metadata TO USER "<user>";
GRANT SELECT ON TABLE <telemetry_schema>.diameter TO USER "<user>";

-- 5. One view per Telemetry table, so that bare names such as flows resolve.
CREATE VIEW u_<user>.flows AS SELECT * FROM <telemetry_schema>.flows;
CREATE VIEW u_<user>.dns_metadata AS SELECT * FROM <telemetry_schema>.dns_metadata;
CREATE VIEW u_<user>.diameter AS SELECT * FROM <telemetry_schema>.diameter;
