// Shared by every Refresh control (a table's name line, a Count Display): POST to the refresh URL and
// return { ok, status, body, error }, where `error` is the plain message to show next to the control.
window.FloodRefresh = {
  // The flow both controls share: `state` is the Alpine component with `refreshing` and `refreshError`.
  // Does nothing (returns null) if a Refresh is already running; otherwise returns post()'s result.
  async run(state, url) {
    if (state.refreshing) return null;
    state.refreshing = true; state.refreshError = "";
    const r = await this.post(url);
    state.refreshing = false;
    if (!r.ok && r.status !== 410) state.refreshError = r.error;
    return r;
  },
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
