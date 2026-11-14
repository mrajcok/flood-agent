// What a SQL Cell's table and column helpers put at the cursor. Pure text functions (tested in tests/js);
// `insertAt` applies them to a textarea.
(function (root) {
  // Words a name can follow without being a select-list item before it (SELECT dport, WHERE dport, ...).
  const LEADERS = new Set(["select", "distinct", "all", "from", "where", "and", "or", "not", "by", "on", "having",
    "when", "then", "else", "case", "in", "is", "like", "glob", "between", "join", "using", "limit", "offset", "with",
    "union", "except", "intersect", "as"]);

  // True if `before` ends with a column name, `*` or `)`: the place a comma may follow.
  function endsInItem(before) {
    const t = before.replace(/\s+$/, "");
    if (/[*)"`\]]$/.test(t)) return true;
    const m = t.match(/[A-Za-z_][A-Za-z0-9_]*$/);
    return !!m && !LEADERS.has(m[0].toLowerCase());
  }

  // `before` (the text before the cursor) with `column` added: after ", " if it ends with a column name, `*` or `)`.
  function afterColumn(before, column) {
    const t = before.replace(/\s+$/, "");
    if (!t) return before + column;
    if (t.endsWith("(")) return t + column;
    if (endsInItem(before)) return t + ", " + column;
    return before + (/\s$/.test(before) ? "" : " ") + column;
  }

  // `before` with `name` added, spaced from the word before it.
  function afterName(before, name) {
    return before + (before && !/\s$/.test(before) ? " " : "") + name;
  }

  // What a table picked in an empty box becomes, and where the cursor goes.
  function startQuery(table) {
    return { text: "SELECT \nFROM " + table, cursor: 7 };
  }

  // Put a column (kind "column") or a table/name (kind "name") into `el` at its cursor, and tell Alpine (input event).
  // A table picked in an empty box (kind "table") starts the query instead.
  function insertAt(el, text, kind) {
    const value = el.value;
    const before = value.slice(0, el.selectionStart), after = value.slice(el.selectionEnd);
    if (kind === "table" && !value.trim()) {
      const q = startQuery(text);
      el.value = q.text;
      el.focus(); el.setSelectionRange(q.cursor, q.cursor);
    } else {
      const head = kind === "column" ? afterColumn(before, text) : afterName(before, text);
      el.value = head + (after && !/^[\s,)]/.test(after) ? " " : "") + after;
      el.focus(); el.setSelectionRange(head.length, head.length);
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  const api = { afterColumn, afterName, startQuery, insertAt };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FloodSql = api;
})(typeof window !== "undefined" ? window : globalThis);
