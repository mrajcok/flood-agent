# work items
- I want to give the user the ability to run an arbitrary SQL SELECT query. The word SELECT will be shown (they can't edit that part) and a "From <table>" part will be shown, where <table> is a drop-down list of every table they have access to with a search feature. The drop-down list should also include a rt_ table name. This is safe since RBAC will prevent them from reading any table they don't have access to. The results should be written to a new rt_* table the the results should be displayed in a table. No LLM should be used for this.
- add the ability to add secondary indexes to a table
- toggle (off by default) for prompt results to go to the LLM
- tests are taking too long to run. We need a way for you to only run targeted tests after a change, instead of running all tests.
- Per J Legg, how can I add workflows to this agent?
  Maybe a "Create workflow" button that opens a dialog with a prompt area and a "Save" button. The prompt area is the same as the main prompt area, but it is saved to a new workflow file in `workflows/` instead of being sent to the LLM. The workflow file is a YAML file with a `prompt` field and an optional `description` field. The workflow can be selected from a dropdown in the main prompt area, and it will replace the current prompt with the workflow's prompt. The workflow can also be edited or deleted from the dropdown.
  The workflow should specify the model, reasoning effort and max query rows.
  When the dialog opens, show a template.
  Maybe have the ability to add an LLM turn to a workflow, and then reorder turns in a workflow.
- more eval. In particular, there were eval errors. Can we adjust the prompts/semantic layer to get better ressults, or do we need to use a better model?
- can the system prompt be made more lean?
- how can we do some kind of progressive disclosure for the semantic layer? E.g. if an analyst is asking about IPFIX data, I don't want to include diameter schema information in the semantic layer part of the system prompt.

# periodically
- run /improve-codebase-architecture with Opus high

# maybe
- validate against the Vega-Lite v5 JSON schema in show_chart. This catches every structural mistake, but it adds a dependency and a large schema file.
- ability to paste an image into the prompt and have it be processed by the LLM.
