# How Ocient types and queries `dns_metadata`'s columns

Research for issue #140 (map #138, adding the `dns_metadata` Telemetry table). Researched 2026-11-16 from the Ocient docs (the `.md` pages under docs.ocient.com, read in full), the pyocient 3.10.1 wheel from PyPI, and Apache Spark's source. **Nothing here has been run against a real Ocient system.** Where the docs are silent, this note says so.

## 1. Where the Spark types land

**How the data gets there.** The docs map *source file* types to Ocient types. Spark doesn't talk to Ocient directly. If the producer writes Parquet, Spark's `ParquetSchemaConverter` ([source](https://github.com/apache/spark/blob/master/sql/core/src/main/scala/org/apache/spark/sql/execution/datasources/parquet/ParquetSchemaConverter.scala)) writes:

| Spark type | Parquet physical type (logical annotation) | Ocient type ([Parquet Type Mapping](https://docs.ocient.com/data-formats-for-data-pipelines.md#parquet-type-mapping)) |
|---|---|---|
| `StringType` | `BINARY` (`STRING`) | `VARCHAR` |
| `ByteType` | `INT32` (`INT(8, signed)`) | `TINYINT` |
| `ShortType` | `INT32` (`INT(16, signed)`) | `SMALLINT` |
| `IntegerType` | `INT32` (none) | `INT` |
| `LongType` | `INT64` (none) | `BIGINT` |

Avro, if the data arrives that way, maps `int` to `INT`, `long` to `BIGINT` and `string` to `VARCHAR` ([Avro Type Mapping](https://docs.ocient.com/data-formats-for-data-pipelines.md#avro-type-mapping)). JSON and delimited fields are always read as `VARCHAR` and cast to the target column ([Data Types for Data Pipelines](https://docs.ocient.com/data-types-for-data-pipelines.md)).

**Ocient has no unsigned integer types.** `TINYINT`/`BYTE` is 1-byte signed (−128..127), `SMALLINT` 2-byte signed (−32768..32767), `INT` 4-byte signed, `BIGINT` 8-byte signed ([Data Types](https://docs.ocient.com/data-types.md)). Spark's `ByteType`, `ShortType`, `IntegerType` and `LongType` are signed as well ([Spark data types](https://spark.apache.org/docs/latest/sql-ref-datatypes.html)). Unsigned Parquet annotations are widened by Ocient: `UINT8` becomes `SMALLINT`, `UINT16` becomes `INT`, `UINT32` becomes `BIGINT`. Spark never writes those, though.

The pipeline auto-promotes to a wider column (`TINYINT` to `SMALLINT`/`INT`/`BIGINT`, `SMALLINT` to `INT`/`BIGINT`, `INT` to `BIGINT`) but never narrows implicitly ([Data Type Promotion List](https://docs.ocient.com/data-types-for-data-pipelines.md#data-type-promotion-list)). So a table column may be wider than the Spark type, but not narrower.

**Per column:**

| Column | Range | Spark type | Fits without sign issues? | Suggested Ocient column |
|---|---|---|---|---|
| `tid` | 0..2³²−1 | `LongType` | Yes. `BIGINT` holds all of it. | `BIGINT` |
| `qtype`, `qclass`, `rtype`, `rclass` | 0..65535 | `IntegerType` | Yes. `INT` holds all of it. | `INT` |
| `sport`, `dport` | 0..65535 | `IntegerType` fits; `ShortType` does **not** | With `IntegerType`, yes. If written as `ShortType`, ports ≥ 32768 are already negative in Spark, before Ocient sees them. | `INT` (Ocient's own docs example types ports as `INT`, [Secondary Indexes](https://docs.ocient.com/secondary-indexes.md)) |
| `proto` | 0..255 | not stated | `ByteType`/`TINYINT` would turn 128..255 negative (IANA assigns numbers above 127). Use `ShortType` or `IntegerType` upstream. | `SMALLINT` or `INT` (`INT` matches `flows.proto` in the Semantic Layer) |
| `opcode`, `rcode`, `flags` | 0..15 | `ByteType` | Yes. 4 bits fit in a signed byte. | `TINYINT`, or wider |
| `rttl` | 32-bit signed | `IntegerType` | Yes. | `INT` |
| `etime` | epoch ms | `LongType` | Yes. | `BIGINT` (see the Time Key note below) |
| `sip`, `dip`, `question`, `rname`, `rdata`, `qr`, `rsection` | text | `StringType` | n/a | `VARCHAR`, or `IP` for the addresses (section 3) |

If a 16-bit field does arrive as `ShortType` (or `proto` as `ByteType`), the pipeline can repair it, for example `CASE WHEN $sport < 0 THEN INT($sport) + 65536 ELSE INT($sport) END`. That is plain SQL, but nobody has tried it in a pipeline.

**Time Key.** `etime` can stay `BIGINT` epoch ms: a TimeKey column "should normally be either a DATE or TIMESTAMP", but "can also support INT or BIGINT type, but these data types do not use a `bucket_value` argument" ([TimeKeys and Clustering Keys](https://docs.ocient.com/timekeys-and-clustering-keys.md)). The docs don't say how segment pruning works on a `BIGINT` TimeKey without buckets.

**Small text columns.** `qr` and `rsection` (one character) fit plain `VARCHAR`. `CHAR(N)` is also variable-length in Ocient and ignores N. Low-cardinality string columns are what Global Dictionary Compression is for ([Global Dictionary Compression](https://docs.ocient.com/global-dictionary-compression.md)). A variable-length column can be in a Clustering Key only if it uses GDC ([TimeKeys and Clustering Keys](https://docs.ocient.com/timekeys-and-clustering-keys.md)).

## 2. Reserved words

None of the 20 column names is reserved. They were checked against all 165 words on [Reserved Words](https://docs.ocient.com/reserved-words.md). The near misses are `TYPE`, `CLASS`, `DATA`, `COUNT`, `COMMENT` and `ROW`. These are reserved, but `qtype`, `rtype`, `qclass`, `rclass` and `rdata` are not. Watch this in generated SQL: an alias such as `AS type`, `AS class` or `AS count` needs double quotes in Ocient. Unquoted identifiers are case-insensitive ([SQL Syntax Conventions](https://docs.ocient.com/sql-syntax-conventions.md)). A live system lists its reserved words in `sys.reserved_words`.

## 3. CIDR tests on IPv4 and IPv6 (what `in_cidr` maps to)

**Types.** `IPV4` is 4 bytes. `IP` is 16 bytes and "can also hold IPV4" ([Data Types](https://docs.ocient.com/data-types.md)). `IP('…')` parses a string holding either family. `IPV4('…')` parses an IPv4 string. `CHAR(ip)` turns either back into a string ([Scalar Data Conversion Functions](https://docs.ocient.com/scalar-data-conversion-functions.md)). Casting IPv4 to `IP` gives "the IPv6-mapped IPv4 address", `::ffff:a.b.c.d` ([Network Type Functions](https://docs.ocient.com/network-type-functions.md)).

**Functions.** [Network Type Functions](https://docs.ocient.com/network-type-functions.md) lists exactly four: `IP`, `IPV4`, `IS_IPV4` and `SUBNET(ip, prefix_size)`. `SUBNET` returns the network prefix as the same type as its argument; the prefix is 0..32 for `IPV4` and 0..128 for `IP`. **There is no CIDR-string parser and no "contains" operator or function** (none in the [Alphabetical SQL Functions List](https://docs.ocient.com/alphabetical-sql-functions-list.md) either). Ocient has no SQL user-defined functions; it only has pipeline functions written in Java ([DDL reference](https://docs.ocient.com/data-definition-language-ddl-statement-reference.md)). So `in_cidr(ip, 'net/len')` has to be rewritten into one of two forms:

```sql
-- (a) prefix equality: IPv4 and IPv6 alike, ip held as a string
SUBNET(IP(sip), 96 + 16) = IP('10.1.0.0')        -- IPv4 10.1.0.0/16: add 96 to the prefix (mapped address)
SUBNET(IP(sip), 32)      = IP('2001:db8::')      -- IPv6 2001:db8::/32

-- (b) range: first and last address of the block
IP(sip) BETWEEN IP('10.1.0.0') AND IP('10.1.255.255')
```

- **Prefix length for IPv4 in `IP` form.** An IPv4 `/n` becomes `/(96+n)`, because the address sits in the low 32 bits of `::ffff:0:0/96`. This follows from the documented mapping but isn't stated or shown in the docs. The alternative is `SUBNET(IPV4(sip), 16) = IPV4('10.1.0.0')`, but `IPV4()` raises `INVALID DATA TYPE CONVERSION` on a real IPv6 address. So on a mixed column, the IPv4 form needs a guard, `IS_IPV4(IP(sip)) AND …`, or must use the `IP` form. The docs don't say whether `AND` short-circuits, so prefer the `IP` form.
- **Speed.** If `sip`/`dip` stay `VARCHAR`, every form parses every row and can't use an index. If they are stored as `IP`, form (b) without the cast (`sip BETWEEN IP('…') AND IP('…')`) is a range filter on a fixed-length column. `INVERTED` and `ZONE_MAP` indexes list `IP / IPV4` with `<`, `>` and `BETWEEN` ([Secondary Indexes](https://docs.ocient.com/secondary-indexes.md)). That also implies `IP` values are ordered, but the docs never state the ordering. Expect it to be byte order, so the mapped IPv4 range sorts together, but test it.
- **Storing as `IP` changes what the app reads back.** pyocient decodes an `IP` column as `ipaddress.ip_address(16 bytes)` (`pyocient/api.py`, 3.10.1), so an IPv4 row comes back as `IPv6Address('::ffff:10.1.2.3')`, whose `str()` is `::ffff:10.1.2.3`. An `IPV4` column comes back as `IPv4Address`. The docs don't say what `CHAR()` of a mapped `IP` value prints (the doc example output shows `128.1.2.3`). Display formats, the hallucination flagger and joins to `flows` (whose `sip`/`dip` are text) would all have to unmap. Keeping `VARCHAR` avoids all of that, at the cost of per-row parsing on CIDR filters.

## 4. String functions for domain names

From [Character and Binary Functions](https://docs.ocient.com/character-and-binary-functions.md) (positions are 1-based; all functions are Unicode-aware):

| Need | Ocient | Notes |
|---|---|---|
| Suffix match | `ENDSWITH(question, '.example.com')`, or `question LIKE '%.example.com'` | In `LIKE`, `_` is a wildcard; escape it as `\_` ([DQL reference](https://docs.ocient.com/data-query-language-dql-statement-reference.md)). Underscores are common in DNS names (`_dmarc`). An `NGRAM` index can serve `LIKE` ([Secondary Indexes](https://docs.ocient.com/secondary-indexes.md)). |
| Prefix match | `STARTSWITH`, `LIKE 'x%'` | |
| Length | `LENGTH` / `CHAR_LENGTH` (characters), `OCTET_LENGTH` (bytes) | |
| Label N from the left | `SPLIT_PART(question, '.', n)` | The docs don't mention negative indexes. |
| Labels as an array, and their count | `SPLIT_TO_ARRAY(question, '.')`, `ARRAY_LENGTH(...)` ([Array Functions](https://docs.ocient.com/array-functions-and-operators.md)) | |
| Label count without arrays | `REGEXP_COUNT(question, '\.') + 1`, or `LENGTH(q) - LENGTH(REPLACE(q, '.', '')) + 1` | The second form also works in SQLite. |
| Last label(s) / parent domain | `REGEXP_SUBSTR(question, '[^.]+\.[^.]+$')`, or `REVERSE(SPLIT_PART(REVERSE(q), '.', 1))` | PCRE syntax is the default. |
| Leftmost label (random-subdomain detection) | `SPLIT_PART(question, '.', 1)`; the rest is `SUBSTRING(q, STRPOS(q, '.') + 1)` | |
| Case folding | `LOWER` | |

**Silent in the docs:**
- Whether `LIKE` is case-sensitive. `ILIKE` is on the reserved-word list but has no documented syntax. DNS names are case-insensitive, and resolvers randomise case (0x20 encoding), so wrap names in `LOWER()` before matching or grouping.
- Whether names carry a trailing dot. This depends on the producer, not on Ocient.

**SQLite (dev).** SQLite has `LENGTH`, `INSTR`, `SUBSTR`, `REPLACE`, `LOWER` and `LIKE`, but not `ENDSWITH`, `SPLIT_PART`, `REVERSE` or `REGEXP_*`. SQLite's `LIKE` is ASCII case-insensitive and has no default escape character. Any Semantic Layer function named after an Ocient built-in would therefore need a SQLite UDF, the way `in_cidr` is done today.

## 5. Joining two large time-partitioned tables on IP/port plus a time window

From [Join Operations](https://docs.ocient.com/join-operations.md):

- **Join type.** A join is a **hash join** when its predicate has at least one equality, otherwise a **product join**: the smaller side is broadcast and every pair is compared. Put the IP and port equalities in `ON` (for example `f.sip = d.dip AND f.sport = d.dport AND f.dip = d.sip AND f.dport = d.sport`). The time window (`d.etime BETWEEN f.etime - w AND f.etime + w`) then runs as a residual filter on the hash-join output. A window-only join would be a product join.
- **The TimeKey does not speed up the join itself.** "Join performance does not generally benefit from … TimeKey, Clustering Keys, secondary indexes, or compression." So bound **each** table on its own `etime` (in `WHERE`, or in a CTE per side) so segment pruning cuts each input before the join. Don't rely on the window predicate alone. The docs don't say whether a range predicate on one side is pushed to the other through the window condition. The "Early Filtering" section mentions dynamic I/O reduction but doesn't define it.
- **Match key types; no functions in join keys.** Mismatched types force implicit conversion, and computed join keys lose statistics and hash efficiency. So `flows.sip` (text) and `dns_metadata.sip` should have the **same type**. If one becomes `IP` and the other stays `VARCHAR`, the join needs `IP(f.sip) = d.dip`, which this page advises against.
- **Memory.** The optimizer orders tables smallest-first, whatever order the SQL gives. A hash join that exceeds memory spills to disk and needs temp space "approximately equal to the combined sizes" of both inputs. Heavy joins affect other queries.
- **Alternative.** The docs suggest window functions in place of self-joins. That doesn't apply to a join of two tables.

## 6. Open questions for a real Ocient system

1. `SUBNET(IP('10.1.2.3'), 112) = IP('10.1.0.0')`: true? (Confirms the +96 rule.)
2. `CHAR(IP('10.1.2.3'))`: `10.1.2.3` or `::ffff:10.1.2.3`? And through pyocient?
3. Does `BETWEEN` on an `IP` column order by bytes, and does it use an `INVERTED`/`ZONE_MAP` index?
4. Is `LIKE` case-sensitive? Does `ILIKE` work?
5. Does a `BIGINT` TimeKey prune segments for `etime >= x AND etime < y` as a `TIMESTAMP` key would?
6. For a flows↔dns join with each side `etime`-bounded, check `EXPLAIN`: a hash join on the IP/port keys, with the window as a filter.
