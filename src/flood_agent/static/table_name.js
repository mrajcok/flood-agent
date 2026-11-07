// The table name header over a table or chart Display (templates/table_name.html): the Result Table's
// name (click to rename it or change its Retention), a copy icon, and the time left before it expires.
//
// It loads its own settings from data-settings-url. After a rename it dispatches `table-renamed`
// ({ old, name }) so the Display around it can follow the new name in its URLs; if the table has
// expired it dispatches `table-expired` and hides.
document.addEventListener("alpine:init", () => {
  Alpine.data("tableName", () => ({
    settingsUrl: "", name: "", retentionDays: 0, expiresInMs: null, gone: false,
    editing: false, draft: "", choice: "", custom: 0, saveError: "",
    originalChoice: "", retentionTouched: false,
    policy: { result_days: 0, saved_days: 0, max_days: 0 },  // the server's Retention policy, set by apply()

    async init() {
      this.settingsUrl = this.$el.dataset.settingsUrl;
      try {
        const r = await fetch(this.settingsUrl);
        if (r.status === 410) return this.expire();
        if (r.ok) this.apply(await r.json());
      } catch (e) { /* no header: the Display itself says what went wrong */ }
    },
    apply(settings) {
      this.name = settings.name; this.retentionDays = settings.retention_days; this.expiresInMs = settings.expires_in_ms;
      this.policy = settings.policy;
    },
    // The Retention choices: the deployment's Result Table default, the Saved Table default, a week and a year.
    get presets() {
      const days = [this.policy.result_days, 7, this.policy.saved_days, 365].filter((d) => d > 0 && d <= this.policy.max_days);
      return [...new Set(days)].sort((a, b) => a - b).map(String);
    },
    presetLabel(days) {
      return FloodFormat.formatRetention(Number(days)) + (Number(days) === this.policy.result_days ? " (rt_ default)" : "");
    },
    expire() { this.gone = true; this.editing = false; this.$dispatch("table-expired"); },
    get expiresIn() { return this.expiresInMs === null ? "" : FloodFormat.formatExpiresIn(this.expiresInMs); },
    get retention() { return FloodFormat.formatRetention(this.retentionDays); },

    edit() {
      this.draft = this.name; this.saveError = "";
      this.choice = this.presets.includes(String(this.retentionDays)) ? String(this.retentionDays) : "custom";
      this.custom = this.retentionDays; this.editing = true;
      this.originalChoice = this.choice; this.retentionTouched = false;
      this.$nextTick(() => { this.$refs.nameInput.focus(); this.$refs.nameInput.select(); });
    },
    renamed() {
      // Renaming a Result Table (rt_*) makes it a Saved Table, kept 30 days unless the user picks
      // a Retention in this edit or it is already kept at least that long (ADR 0004).
      if (this.retentionTouched || !this.name.toLowerCase().startsWith("rt_") || this.retentionDays >= this.policy.saved_days) return;
      this.choice = this.draft.trim() === this.name ? this.originalChoice : String(this.policy.saved_days);
    },
    async save() {
      const days = this.choice === "custom" ? this.custom : this.choice;
      const body = new URLSearchParams({ name: this.draft, retention_days: String(days) });
      try {
        const r = await fetch(this.settingsUrl, { method: "POST", body });
        if (r.status === 410) return this.expire();
        if (!r.ok) {
          this.saveError = r.status === 422
            ? ((await r.json()).detail || "Enter a valid name and a number of days between 0 and " + this.policy.max_days + ".") : "Could not save.";
          return;
        }
        const old = this.name;
        this.apply(await r.json());
        this.editing = false;
        if (this.name !== old) {
          // A rename changes the table's name in every URL.
          this.settingsUrl = this.settingsUrl.replace("/api/results/" + encodeURIComponent(old) + "/",
                                                      "/api/results/" + encodeURIComponent(this.name) + "/");
          this.$dispatch("table-renamed", { old, name: this.name });
        }
      } catch (e) { this.saveError = "Could not save."; }
    },
  }));
});
