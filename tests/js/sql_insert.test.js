const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../../src/flood_agent/static/sql_insert.js");

// The text before the cursor goes in; the text before the cursor, with the new item added, comes out.
test("a column after SELECT, a comma or a keyword takes no comma", () => {
  assert.equal(S.afterColumn("SELECT ", "dport"), "SELECT dport");
  assert.equal(S.afterColumn("SELECT", "dport"), "SELECT dport");
  assert.equal(S.afterColumn("SELECT sip, ", "dport"), "SELECT sip, dport");
  assert.equal(S.afterColumn("SELECT DISTINCT ", "dport"), "SELECT DISTINCT dport");
  assert.equal(S.afterColumn("SELECT sip FROM flows WHERE ", "dport"), "SELECT sip FROM flows WHERE dport");
  assert.equal(S.afterColumn("", "dport"), "dport");
});

test("a column after a column name, * or ) is preceded by a comma", () => {
  assert.equal(S.afterColumn("SELECT sip", "dport"), "SELECT sip, dport");
  assert.equal(S.afterColumn("SELECT sip ", "dport"), "SELECT sip, dport");
  assert.equal(S.afterColumn("SELECT *", "dport"), "SELECT *, dport");
  assert.equal(S.afterColumn("SELECT count(*)", "dport"), "SELECT count(*), dport");
  assert.equal(S.afterColumn('SELECT "analyze"', "dport"), 'SELECT "analyze", dport');
});

test("a column inside a call or after an operator takes no comma", () => {
  assert.equal(S.afterColumn("SELECT sum(", "bytes"), "SELECT sum(bytes");
  assert.equal(S.afterColumn("SELECT bytes +", "pkts"), "SELECT bytes + pkts");
  assert.equal(S.afterColumn("SELECT sip FROM flows GROUP BY ", "dport"), "SELECT sip FROM flows GROUP BY dport");
});

test("a name is spaced from the words around it", () => {
  assert.equal(S.afterName("SELECT sip FROM", "flows"), "SELECT sip FROM flows");
  assert.equal(S.afterName("SELECT sip FROM ", "flows"), "SELECT sip FROM flows");
  assert.equal(S.afterName("", "flows"), "flows");
});

test("a table picked in an empty box starts SELECT and FROM, the cursor after SELECT", () => {
  assert.deepEqual(S.startQuery("flows"), { text: "SELECT \nFROM flows", cursor: 7 });
});
