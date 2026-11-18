# DNS fields in streamsx.network (flags, sections, rdata, names)

Research for the `dns_metadata` table (map #138, ticket #145). Researched 2026-11-17 by reading the source of [IBMStreams/streamsx.network](https://github.com/IBMStreams/streamsx.network), branch `develop` at commit [`3f1672a`](https://github.com/IBMStreams/streamsx.network/tree/3f1672a2f349176fbbfa30d6937063281bd42c36) (2020-11-13, the last commit). The other branches (`rtp-dev`, `v3.0.x_m`, `incubation`) hold the same DNS parser code or older versions of it. **Nothing was built or run.** The collector itself is not in this repo: it is a separate application built on the toolkit. So this file can only say what the toolkit's functions return. Anything about what the collector does with those values is marked as inference.

Links below are permalinks at `3f1672a`. Short names used:

- **Parser**: [`impl/include/parse/DNSMessageParser.h`](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h), the C++ class behind every `DNS_*` function.
- **Docs**: [`com.ibm.streamsx.network.dns/native.function/function.xml`](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/native.function/function.xml), the SPL documentation of the `DNS_*` functions. The `parse` namespace has a copy for the `DNSMessageParser` operator.
- **Operator**: [`DNSPacketFileSource_h.cgt`](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/DNSPacketFileSource/DNSPacketFileSource_h.cgt), where each `DNS_*` function is a one-line call into the Parser. The Live, DPDK and `DNSMessageParser` operators have the same lines.
- **Flattener**: [`impl/include/dns/DNSPacketFlattener.h`](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/dns/DNSPacketFlattener.h), behind `DNS_ALL_FIELDS()`.
- **Misc**: [`impl/include/MiscFunctions.h`](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/MiscFunctions.h), the `\HH` escaping functions.

## 1. `DNS_ALL_FLAGS` returns all 16 header bits, not 4

- **Source.** `DNS_ALL_FLAGS() { return ntohs(parser.dnsHeader->flags.allFlags); }` ([Operator L263](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/DNSPacketFileSource/DNSPacketFileSource_h.cgt#L263)). It returns the whole 16-bit flags word of the DNS header in host order, so values run from 0 to 65535. The docs say the same: "a 16-bit value containing all of the DNS header flags", with `& 0x8000` = QR, `>> 11 & 0x000F` = opcode, `& 0x000F` = RCODE ([Docs L573-L582](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/native.function/function.xml#L573-L582)).
- **Bit layout.** This is the standard RFC 1035/4035 layout, as the header struct shows ([Parser L86-L117](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L86-L117); the field names are the toolkit's):

  | mask | field | toolkit name |
  |---|---|---|
  | `0x8000` | QR | `responseFlag` |
  | `0x7800` | Opcode | `opcodeField` |
  | `0x0400` | AA | `authoritativeFlag` |
  | `0x0200` | TC | `truncatedFlag` |
  | `0x0100` | RD | `recursionDesiredFlag` |
  | `0x0080` | RA | `recursionAvailableFlag` |
  | `0x0040` | Z | `reserved` |
  | `0x0020` | AD | `authenticatedFlag` |
  | `0x0010` | CD | `nonauthenticatedFlag` |
  | `0x000F` | RCODE | `responseCode` |

- **The source never packs 4 bits.** No function in the toolkit packs four flags into a value from 0 to 15. A typical recursive NOERROR response (QR, RD and RA set) gives `DNS_ALL_FLAGS()` = `0x8180` = 33152. So the collector's 0–15 `flags` value is **not the raw output of `DNS_ALL_FLAGS()`**. The collector must reduce it somehow, and the source cannot say how. Two readings fit the bits (both are inference):
  - **`(DNS_ALL_FLAGS() >> 7) & 0xF`**: the four adjacent single-bit flags AA, TC, RD, RA (bits 10–7). This gives **AA = 8, TC = 4, RD = 2, RA = 1**, the mapping the ticket guessed. It also fits the claim that AA and TC are among the four bits.
  - **`DNS_ALL_FLAGS() & 0xF`**: the docs' own example mask. This is the **RCODE**, not flags.
  - **How to tell them apart in production data.** Under the first reading, ordinary recursive answers show `3` (RD + RA). Under the second they show `0` (NOERROR), and since NXDOMAIN rows are absent, `3` would never appear.
- **Error codes 001–004 are `DNS_INCOMPATIBLE_FLAGS()`.** The codes 1–4 that the collector spec cites as evidence for AA and TC come from `DNS_INCOMPATIBLE_FLAGS()` ([Parser L1018-L1028](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L1018-L1028), [Docs L913-L928](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/native.function/function.xml#L913-L928)). The codes are: 1 = AA without QR, 2 = TC without QR, 3 = AA and TC, 4 = AA, TC, RD and RA. A code comment calls this "column 23 of the 'flattened' DNS format". That format is not defined anywhere in the repo. **Code 4 can never be returned**: whenever its condition holds, the code-3 test (AA and TC) runs first and returns 3. So these codes tell us which bits the toolkit checks (QR, AA, TC, RD, RA). They say nothing about how `flags` is packed.

## 2. RR section codes: the toolkit has none

- **No section codes in the source.** No single-character section code (`N`, `T`, `D`, or anything else) appears in the toolkit, on any branch. The toolkit gives each section its own family of list functions:
  - question: `DNS_QUESTION_*`
  - answer: `DNS_ANSWER_*`
  - authority: `DNS_NAMESERVER_*`
  - additional: `DNS_ADDITIONAL_*`

  See [Operator L266-L365](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/DNSPacketFileSource/DNSPacketFileSource_h.cgt#L266-L365). `DNS_ALL_FIELDS()` labels records `question.i`, `answer.i`, `nameserver.i` and `additional.i` ([Flattener L426-L492](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/dns/DNSPacketFlattener.h#L426-L492)).
- **The codes are the collector's own.** A row with an `rsection` column is something the collector builds by walking these lists, so the code list is defined there, not in the toolkit.
- **Inference: `N` may mean "nameserver".** In the toolkit's vocabulary the authority section is the **"nameserver"** section. So `N` could just as well be **Nameserver (authority)** as "aNswer". The `N`/`T`/`D` mapping in #139 stays unconfirmed, and the toolkit gives a reason to doubt it. Only the collector's code, or production rows checked against known responses, can settle it. One check works: for an answer-section row, `rname` equals `question` or follows its CNAME chain; for an authority-section row, `rtype` is NS or SOA and `rname` is a parent zone.

## 3. `DNS_ANSWER_DATA()` text per record type

- **Source.** `DNS_ANSWER_DATA()` returns a `list<rstring>` with one entry per answer record ([Operator L302](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/DNSPacketFileSource/DNSPacketFileSource_h.cgt#L302)). Each entry comes from `convertResourceDataToString()` ([Parser L947-L991](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L947-L991)). `DNS_NAMESERVER_DATA()` and `DNS_ADDITIONAL_DATA()` use the same function. Their one-argument overloads add a delimiter that only SOA uses ([Docs L807-L878](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/native.function/function.xml#L807-L878)). `DNS_ANSWER_DATA` has **no** delimiter overload.

  | type | text produced | source |
  |---|---|---|
  | A (1) | dotted quad, `inet_ntop(AF_INET)`, e.g. `93.184.216.34` | [L957](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L957), [L895-L919](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L895-L919) |
  | AAAA (28) | `inet_ntop(AF_INET6)`. With glibc this is the compressed lowercase form (`2606:2800:220:1::248`; `::ffff:1.2.3.4` for v4-mapped). The case and compression come from libc, not the toolkit. | [L971](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L971) |
  | NS (2), CNAME (5), PTR (12) | the target domain name, in the name format of section 4 | [L958-L963](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L958-L963) |
  | MX (15) | **exchange name only**; the preference is dropped | [L966](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L966) |
  | SRV (33) | **target name only**; priority, weight and port are dropped | [L972](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L972) |
  | SOA (6) | Without a delimiter (always the case for `DNS_ANSWER_DATA()`): **MNAME only**, e.g. `a.dns.ripn.net`. With a delimiter `d`: `mname d rname d serial d refresh d retry d expire d minimum`, the numbers as unsigned decimal. | [L960](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L960), [L791-L836](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L791-L836) |
  | TXT (16) | **first character-string only**, raw bytes without its length byte; any later strings are dropped | [L967](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L967), [L768-L788](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L768-L788) |
  | AFSDB (18) | hostname only | L968 |
  | SPF (99), NULL (10) | the whole rdata as raw bytes; for SPF this **includes** the length bytes | L961, L985 |
  | OPT / EDNS0 (41) | empty string | L974 |
  | WKS, HINFO, MINFO, SIG, KEY, NAPTR, DS, SSHFP, IPSECKEY, RRSIG, NSEC, DNSKEY, NSEC3, NSEC3PARAM, TLSA, CDNSKEY, TKEY, TSIG | a fixed placeholder such as `[RRSIG data]` or `[DS data]` | L962-L987 |
  | any other type (e.g. SVCB 64, HTTPS 65, CAA 257) | the whole rdata as **raw binary bytes** | [L988](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L988) |

- **Caveats.**
  - The Parser's converter does not check `rdlength == 0`. The Flattener's does ([Flattener L349](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/dns/DNSPacketFlattener.h#L349)).
  - Several Flattener comments say it "fold[s] uppercase characters to lowercase", but no code does any folding.
- **The SOA hint is explained.** #139's hint that SOA "may be reduced to the primary nameserver" is exactly what the no-delimiter path does. It applies to every SOA from `DNS_ANSWER_DATA()`, and to SOAs from `DNS_NAMESERVER_DATA()` / `DNS_ADDITIONAL_DATA()` when they are called without a delimiter.
- **The `^` separator is not in the `DNS_*` functions.** They return SPL lists, not joined strings. `^` appears in the toolkit in two places:
  1. **As a delimiter in the samples.** The samples call `DNS_ALL_FIELDS("\n", "|", "^")`: records split by newline, fields by `|`, SOA/MX/SRV subfields by `^` ([sample L109-L124](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/samples/SampleDNSPacketDPDKSource/sample/LiveDNSPacketDPDKSourceFilter.spl#L109-L124)). The collector's `|`-delimited files with `^` inside a field follow the same convention.
  2. **Through `stringEscapeConcat`.** The `com.ibm.streamsx.network.misc` functions `stringEscape(list, d1, d2, d3)` and `stringEscapeConcat(list, subfieldDelim, d1, d2, d3)` ([Misc L40-L89](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/MiscFunctions.h#L40-L89); their SPL docs read "TODO: Describe." in [misc function.xml](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.misc/native.function/function.xml)). `stringEscapeConcat` joins a list with `subfieldDelim` and escapes each element.

  **Inference.** The collector most likely builds `rdata` (and escapes names) with these functions, passing `^` and `|` as delimiters. Which values end up `^`-joined inside one per-record row is the collector's choice. Possibilities include SOA subfields through `DNS_NAMESERVER_DATA("^")`, or several records joined. The source cannot tell.
- **The `\HH` escaping rule.** `moveEscaped()` ([Misc L14-L38](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/MiscFunctions.h#L14-L38)) replaces a byte with a backslash and two **uppercase** hex digits when the byte is any of:
  - a control byte, `< 0x20`
  - `> 0x7E`, which includes 0x7F
  - a backslash itself
  - one of up to three delimiter bytes passed by the caller

  So `^` becomes `\5E`, `|` becomes `\7C`, a backslash becomes `\5C`, and byte 0xC3 becomes `\C3`. Every other byte passes through unchanged, and there is no other escape syntax (no `\.` or `\DDD` as in zone files).

  **The `DNS_*` functions themselves never escape.** `decodeDNSEncodedName()` has an `escapeNonPrintable` flag that uses the same rule ([Parser L683-L719](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L683-L719)), but nothing in the toolkit sets it, so raw bytes come out. Any `\HH` in production comes from the collector, presumably through `stringEscape`/`stringEscapeConcat` (inference). Given the table above, TXT and unknown-type rdata (HTTPS, SVCB, CAA) will be the heavily escaped values.

## 4. Name format (question and record names)

All names, including names inside rdata, go through `decodeDNSEncodedName()` ([Parser L686-L746](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L686-L746)) via `convertDNSEncodedNameToString()` (L755-L765).

- **Case is preserved.** Label bytes are copied with `memcpy` (L715) and never lowercased. So `WwW.ExAmPlE.CoM` (for example 0x20-randomised queries) arrives as sent.
- **No trailing dot.** Labels are joined with `.`, and the final dot is removed (L717, L745). The **root name becomes the empty string**. So the OPT pseudo-record in the additional section has an empty name and, from section 3, empty rdata. That holds only if the collector emits additional-section rows (inference).
- **No escaping in the toolkit.** A `.` or `|` inside a label is not escaped and cannot be told apart from a separator (see section 3 for escaping). IDNs are whatever bytes are on the wire, normally punycode `xn--` labels. Neither of these is specific to the toolkit.
- **Question name.** `DNS_QUESTION_NAME()` is the first question only. `DNS_QUESTION_NAMES()` lists all of them; the parser keeps at most 10 ([Parser L657-L659](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L657-L659)).
- **Long or broken names.** Decoding stops once the name passes 253 characters, or on a bad label, and returns a truncated name with an error set (L698-L741). The docs warn that names "may be empty or truncated" when `parseError()` is true ([Docs L590-L600](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/com.ibm.streamsx.network.dns/native.function/function.xml#L590-L600)).

## 5. Responses with no records (NXDOMAIN, NODATA)

- **The functions still return values.** The parser handles a message with zero answer records normally. `DNS_ANSWER_*` return **empty lists** and `DNS_ANSWER_COUNT()` is 0. The header functions and `DNS_QUESTION_*` still return values. Any authority-section records are still in the `DNS_NAMESERVER_*` lists: NXDOMAIN and NODATA responses usually carry an SOA there. See [Parser L1049-L1113](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/com.ibm.streamsx.network/impl/include/parse/DNSMessageParser.h#L1049-L1113).
- **Whether rows come out is up to the collector.** The source operators emit **one tuple per packet** that passes their `outputFilters`. With one row per resource record, a response yields zero rows only if the collector expands nothing for it.
- **Inference: NXDOMAIN may be filtered by response code.** The toolkit sample sends responses to separate streams by RCODE: `FlattenedDNSNormalResponseStream` for `DNS_RESPONSE_CODE()==0` and `FlattenedDNSErrorResponseStream` for the rest ([sample L85-L92](https://github.com/IBMStreams/streamsx.network/blob/3f1672a2f349176fbbfa30d6937063281bd42c36/samples/SampleDNSPacketDPDKSource/sample/LiveDNSPacketDPDKSourceFilter.spl#L85-L92)). If the collector follows this pattern, NXDOMAIN (RCODE 3) goes to an error-response output and never reaches `dns_metadata`. That would also explain the separate "NOR" (normal response?) output in the collector spec. **NODATA**, however, is RCODE 0. If the collector emits authority-section rows, a NODATA response would produce one SOA row (in whatever section code means authority) and no answer rows. If it emits only answer rows, it would produce nothing. The source cannot decide this.
- **Parse errors give partial output.** On an error, parsing stops at the bad record and keeps the records parsed so far. Later sections then have zero records (L1096-L1100). So a malformed response can produce answer rows but no authority or additional rows.
- **Messages the parser refuses.** These give **no records at all**, with an error code:
  - a section count over 150 (error 117, L1075-L1078; limits at L657 and L667)
  - any rdata longer than 1500 bytes (error 123, L203)

## 6. Summary for the generator

| question | answer from source | confidence |
|---|---|---|
| `flags` 0–15 bits | `DNS_ALL_FLAGS()` is the full 16-bit word; the 0–15 reduction is the collector's. The likely reading is AA = 8, TC = 4, RD = 2, RA = 1 (bits 10–7). If it is the low nibble instead, it is RCODE. | inference; check production values (mostly `3` vs mostly `0`) |
| `rsection` codes | not in the toolkit; the collector defines them. The toolkit calls authority "nameserver", so `N` may be authority, not answer. | unknown |
| `rdata` formats | per the table in section 3: A dotted quad, AAAA `inet_ntop`, MX/SRV target name only, SOA MNAME only, TXT first string only, DNSSEC types as placeholders, unknown types as raw bytes | source |
| `^` and `\HH` | `\HH` = backslash plus uppercase hex, for bytes `< 0x20`, `> 0x7E`, backslash and caller-given delimiters (`^` becomes `\5E`); applied by the collector through `stringEscape*` | rule from source; that the collector uses it is inference |
| names | case preserved, no trailing dot, root name = empty string, not escaped by the toolkit | source |
| no-record responses | the functions return empty lists, not "no output"; zero rows is the collector's doing, probably an RCODE filter | inference |
