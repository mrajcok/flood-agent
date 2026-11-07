# work items
- when a turn is collapsed and expanded, animate it like the live trace. Same for when a completed trace is opened and closed.
- more keystrokes supported in the prompt area: ctrl-a to move the cursot to the beginning of the prompt, ctrl-e to move the cursor to the end of the prompt, ctrl-k to delete from the cursor to the end of the prompt, ctrl-u to delete from the cursor to the beginning of the prompt, ctrl-w to delete the word before the cursor, ctrl-d to delete the character under the cursor, ctrl-h to delete the character before the cursor, ctrl-y to paste from clipboard, ctrl-l to clear the prompt area.
- add a toggle at the top to show/hide all traces. This is useful when there are many traces and the user wants to focus on the prompt and results. When traces are hidden, live traces are also not shown. Or should that be an additional toggle to hide live traces?
- should there be a keystroke that can stop the LLM from running? (e.g. if it is taking too long)
- tests are taking too long to run. We need a way for you to only run targeted tests after a change, instead of running all tests.

- file upload for lookup table
- ability to update a query if has a "in the last 4 hours" kind of time range
- in LLM step · 0 ms · 11.6 KB sent · 0 in / 0 out · $0.000, what does 0 in, 0 out mean? Is 0 in the same as 11.6 KB sent?
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
