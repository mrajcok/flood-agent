// Pure display-formatting functions. Rules come from the Semantic Layer (the server
// sends per-column formats); nothing here uses local time.
(function (root) {
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function pad(n, w) { return String(n).padStart(w || 2, "0"); }

  function formatBytes(v) {
    if (v === null || v === undefined) return "";
    if (!isNum(v)) return String(v);
    const units = ["KB", "MB", "GB", "TB"];
    if (Math.abs(v) < 1000) return v + " B";
    let n = v, i = -1;
    do { n /= 1000; i++; } while (Math.abs(n) >= 1000 && i < units.length - 1);
    return n.toFixed(2) + " " + units[i];
  }

  function formatTime(v) {
    if (v === null || v === undefined) return "";
    if (!isNum(v)) return String(v);
    const d = new Date(v);
    const ms = d.getUTCMilliseconds();
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " +
      pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) +
      (ms ? "." + pad(ms, 3) : "") + " UTC";
  }

  function formatEnum(v, map) {
    if (v === null || v === undefined) return "";
    return (map && map[v] !== undefined) ? map[v] : String(v);
  }

  function formatFlags(v, map) {
    if (v === null || v === undefined) return "";
    if (!isNum(v)) return String(v);
    const bits = Object.keys(map || {}).map(Number).sort((a, b) => a - b);
    const out = bits.filter((b) => (v & b) !== 0).map((b) => String(map[b]).charAt(0).toUpperCase());
    return out.length ? out.join("") : "-";
  }

  function formatCell(v, fmt) {
    const kind = fmt && fmt.kind;
    if (kind === "bytes") return formatBytes(v);
    if (kind === "time") return formatTime(v);
    if (kind === "enum") return formatEnum(v, fmt.map);
    if (kind === "flags") return formatFlags(v, fmt.map);
    return v === null || v === undefined ? "" : String(v);
  }

  // Right-align a column of numbers, but not enum/flags codes, which are displayed as text.
  function isNumericColumn(rows, j, fmt) {
    const kind = fmt && fmt.kind;
    if (kind === "enum" || kind === "flags") return false;
    const values = rows.map((r) => r[j]).filter((v) => v !== null && v !== undefined);
    return values.length > 0 && values.every(isNum);
  }

  // A Saved or Result Table's Retention in days: under a day reads better in hours, under an hour in minutes.
  function formatRetention(days) {
    const [n, unit] = days >= 1 ? [Math.round(days * 100) / 100, "day"]
      : days * 24 >= 1 ? [Math.round(days * 240) / 10, "hour"] : [Math.max(1, Math.round(days * 1440)), "minute"];
    return n + " " + unit + (n === 1 ? "" : "s");
  }

  // Time left before a table expires, to the nearest hour, or the nearest day from a week on.
  function formatExpiresIn(ms) {
    const hours = Math.round(ms / 3600000);
    if (hours < 1) return "expires in under 1h";
    if (hours >= 7 * 24) return "expires in " + Math.round(ms / 86400000) + "d";  // from ms: rounding twice drifts
    const d = Math.floor(hours / 24), h = hours % 24;
    return "expires in " + [d && d + "d", h && h + "h"].filter(Boolean).join(" ");
  }

  const api = { isNumericColumn, formatBytes, formatTime, formatEnum, formatFlags, formatCell, formatRetention, formatExpiresIn };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FloodFormat = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
