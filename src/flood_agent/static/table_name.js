// The table name header over a table or chart Display (templates/table_name.html): the Result Table's
// name (click to rename it or change its Retention), a copy icon, and the time left before it expires.
//
// It loads its own settings from data-settings-url. After a rename it dispatches `table-renamed`
// ({ old, name }) so the Display around it can follow the new name in its URLs; if the table has
// expired it dispatches `table-expired` and hides.
document.addEventListener("alpine:init", () => {
  Alpine.data("tableName", () => ({
    settingsUrl: "", name: "", retentionDays: 1, expiresInMs: null, gone: false,
    editing: false, draft: "", choice: "1", custom: 1, saveError: "",
    originalChoice: "1", retentionTouched: false,
    presets: ["1", "7", "30", "365"],

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
      if (this.retentionTouched || !this.name.toLowerCase().startsWith("rt_") || this.retentionDays >= 30) return;
      this.choice = this.draft.trim() === this.name ? this.originalChoice : "30";
    },
    async save() {
      const days = this.choice === "custom" ? this.custom : this.choice;
      const body = new URLSearchParams({ name: this.draft, retention_days: String(days) });
      try {
        const r = await fetch(this.settingsUrl, { method: "POST", body });
        if (r.status === 410) return this.expire();
        if (!r.ok) {
          this.saveError = r.status === 422
            ? ((await r.json()).detail || "Enter a valid name and a number of days between 0 and 3650.") : "Could not save.";
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
