# work items
- report mode: removes all buttons and cell borders, and makes the notebook read-only. Maybe a "Report" button in the top area that toggles this mode.
- toggle (off by default) for prompt results to go to the LLM
- Per J Legg, how can I add workflows to this agent?
  Maybe a "Create workflow" button that opens a dialog with a prompt area and a "Save" button. The prompt area is the same as the main prompt area, but it is saved to a new workflow file in `workflows/` instead of being sent to the LLM. The workflow file is a YAML file with a `prompt` field and an optional `description` field. The workflow can be selected from a dropdown in the main prompt area, and it will replace the current prompt with the workflow's prompt. The workflow can also be edited or deleted from the dropdown.
  The workflow should specify the model, reasoning effort and max query rows.
  When the dialog opens, show a template.
  Maybe have the ability to add an LLM turn to a workflow, and then reorder turns in a workflow.
- Workflows (saved, re-runnable prompt sequences). The cell model keeps them possible, but nothing was designed or built.
- Run all / Run all below. This is the start of the workflow idea, and #106 ruled it out.
- more eval. In particular, there were eval errors. Can we adjust the prompts/semantic layer to get better ressults, or do we need to use a better model?
- can the system prompt be made more lean?
- how can we do some kind of progressive disclosure for the semantic layer? E.g. if an analyst is asking about IPFIX data, I don't want to include diameter schema information in the semantic layer part of the system prompt.

# periodically
- run /improve-codebase-architecture with Opus high

# maybe
- validate against the Vega-Lite v5 JSON schema in show_chart. This catches every structural mistake, but it adds a dependency and a large schema file.
- ability to paste an image into the prompt and have it be processed by the LLM.
- Export / Import of a notebook as JSON.
- Toggle to send prompt results to the LLM. It conflicts with ADR 0001.
- sql cell should also have a "Send to AI" checkbox. It should be ticked by default.
- add the ability to add secondary indexes to a table

# Possible project names
Acronym	Expansion	Best Vibe
F-NATCH	- Notebook for AI, Telemetry, Chat, and Hunting;	Closest phonetic match to F-WATCH
F-NATS	- Notebook for AI, Telemetry, and SQL;	Most accurate to your exact tech stack
F-NASH	- Notebook for AI, SQL, and Hunting;	Best blend of SQL querying and threat hunting
F-CANOPY — Cyber AI Notebook for Operations, Peering, and telemetrY. Suggests overarching visibility across carrier networks and peering borders.