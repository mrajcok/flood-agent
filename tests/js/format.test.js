const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("../../src/flood_defender/static/format.js");

test("bytes use decimal KB/MB/GB/TB", () => {
  assert.equal(F.formatBytes(0), "0 B");
  assert.equal(F.formatBytes(999), "999 B");
  assert.equal(F.formatBytes(1000), "1.00 KB");
  assert.equal(F.formatBytes(987654), "987.65 KB");
  assert.equal(F.formatBytes(1500000), "1.50 MB");
  assert.equal(F.formatBytes(2.5e9), "2.50 GB");
  assert.equal(F.formatBytes(3e12), "3.00 TB");
  assert.equal(F.formatBytes(null), "");
  assert.equal(F.formatBytes("abc"), "abc");
});

test("timestamps are UTC regardless of process timezone", () => {
  assert.equal(F.formatTime(1767225600000), "2026-01-01 00:00:00 UTC");
  assert.equal(F.formatTime(1700000000111), "2023-11-14 22:13:20.111 UTC");
  assert.equal(F.formatTime(null), "");
});

test("enum names fall back to the number", () => {
  const m = { 6: "TCP", 17: "UDP" };
  assert.equal(F.formatEnum(6, m), "TCP");
  assert.equal(F.formatEnum(99, m), "99");
});

test("tcp flags render as letters in bit order", () => {
  const m = { 1: "FIN", 2: "SYN", 4: "RST", 8: "PSH", 16: "ACK", 32: "URG" };
  assert.equal(F.formatFlags(2, m), "S");
  assert.equal(F.formatFlags(18, m), "SA");
  assert.equal(F.formatFlags(25, m), "FPA");
  assert.equal(F.formatFlags(0, m), "-");
  assert.equal(F.formatFlags(2 | 64, m), "S");
});

test("formatCell dispatches on column format", () => {
  assert.equal(F.formatCell(18, { kind: "flags", map: { 2: "SYN", 16: "ACK" } }), "SA");
  assert.equal(F.formatCell(17, { kind: "enum", map: { 17: "UDP" } }), "UDP");
  assert.equal(F.formatCell(5, { kind: "plain" }), "5");
  assert.equal(F.formatCell(null, { kind: "bytes" }), "");
  assert.equal(F.formatCell(1000, undefined), "1000");
});

test("a column is numeric when its non-null values are all numbers shown as numbers", () => {
  assert.equal(F.isNumericColumn([[1], [2.5], [null]], 0, { kind: "plain" }), true);
  assert.equal(F.isNumericColumn([[1000], [2000]], 0, { kind: "bytes" }), true);
  assert.equal(F.isNumericColumn([[1767225600000]], 0, { kind: "time" }), true);
  assert.equal(F.isNumericColumn([[1], [2]], 0, undefined), true);
});

test("enum and flags codes are shown as text, so their columns are not numeric", () => {
  assert.equal(F.isNumericColumn([[6], [17]], 0, { kind: "enum", map: { 6: "TCP" } }), false);
  assert.equal(F.isNumericColumn([[2], [18]], 0, { kind: "flags", map: { 2: "syn" } }), false);
});

test("text, mixed and all-null columns are not numeric", () => {
  assert.equal(F.isNumericColumn([["10.0.0.1"], ["10.0.0.2"]], 0, { kind: "plain" }), false);
  assert.equal(F.isNumericColumn([[1], ["x"]], 0, { kind: "plain" }), false);
  assert.equal(F.isNumericColumn([[null], [null]], 0, { kind: "plain" }), false);
  assert.equal(F.isNumericColumn([], 0, { kind: "plain" }), false);
});
