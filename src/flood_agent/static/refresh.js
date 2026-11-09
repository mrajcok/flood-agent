// Shared by every Refresh control (a table's name line, a Count Display): POST to the refresh URL and
// return { ok, status, body, error }, where `error` is the plain message to show next to the control.
window.FloodRefresh = {
  async post(url) {
    try {
      const r = await fetch(url, { method: "POST" });
      let body = null;
      try { body = await r.json(); } catch (e) { /* not JSON */ }
      if (r.ok) return { ok: true, status: r.status, body, error: "" };
      const detail = body && body.detail;
      return { ok: false, status: r.status, body, error: typeof detail === "string" && detail ? detail : "Could not refresh." };
    } catch (e) {
      return { ok: false, status: 0, body: null, error: "Could not refresh." };
    }
  },
};
