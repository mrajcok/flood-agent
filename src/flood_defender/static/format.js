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

  const api = { formatBytes, formatTime, formatEnum, formatFlags, formatCell };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.FloodFormat = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
