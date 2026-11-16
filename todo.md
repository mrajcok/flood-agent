# work items
- AI cell: add a toggle (off by default) for prompt results/answers to go to the LLM. Are there cases we want the LLM to see the data? What if the data is sensitive?
- Run all / Run all below. This is the start of the workflow idea, and #106 ruled it out.
- more eval. In particular, there were eval errors. Can we adjust the prompts/semantic layer to get better ressults, or do we need to use a better model?
- can the system prompt be made more lean?
- how can we do some kind of progressive disclosure for the semantic layer? E.g. if an analyst is asking about IPFIX data, I don't want to include diameter schema information in the semantic layer part of the system prompt. Maybe Ocient's Defender already does this.
- certain tables shouldn't be allowed to be downloaded because they may contain data that needs to be encrypted at rest.

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