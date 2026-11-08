# work items
- should there be a keystroke that can stop the LLM from running? (e.g. if it is taking too long)
- tests are taking too long to run. We need a way for you to only run targeted tests after a change, instead of running all tests.

- ability to update a query if has a "in the last 4 hours" kind of time range
- Per J Legg, how can I add workflows to this agent?
  Maybe a "Create workflow" button that opens a dialog with a prompt area and a "Save" button. The prompt area is the same as the main prompt area, but it is saved to a new workflow file in `workflows/` instead of being sent to the LLM. The workflow file is a YAML file with a `prompt` field and an optional `description` field. The workflow can be selected from a dropdown in the main prompt area, and it will replace the current prompt with the workflow's prompt. The workflow can also be edited or deleted from the dropdown.
  The workflow should specify the model, reasoning effort and max query rows.
  When the dialog opens, show a template.
- more eval. In particular, there were eval errors. Can we adjust the prompts/semantic layer to get better ressults, or do we need to use a better model?
- can the system prompt be made more lean?
- how can we do some kind of progressive disclosure for the semantic layer? E.g. if an analyst is asking about IPFIX data, I don't want to include diameter schema information in the semantic layer part of the system prompt.

# periodically
- run /improve-codebase-architecture with Opus high

# maybe
- add a view button to the results table when it is above a chart. Open a dialog to show it.
- validate against the Vega-Lite v5 JSON schema in show_chart. This catches every structural mistake, but it adds a dependency and a large schema file.
- ability to paste an image into the prompt and have it be processed by the LLM.
