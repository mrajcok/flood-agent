"""Eval harness helpers over the Notebook API: cells above a question, and a question as an AI Cell."""

from collections.abc import AsyncIterator

from flood_agent.agent import Agent, Event


async def add_cells_above(agent: Agent, notebook_id: str, above: list[dict] | None) -> None:
    """Append the cells a golden question has above it, top to bottom. SQL and SHOW cells are run, so that the
    AI Cell below is sent what they ran; Markdown cells are only added (a ticked one is sent)."""
    for spec in above or []:
        kind = spec["kind"]
        if kind == "markdown":
            agent.add_markdown_cell(notebook_id, spec["text"], send_to_ai=bool(spec.get("send_to_ai")))
            continue
        if kind == "sql":
            cell_id = agent.add_sql_cell(notebook_id, spec["sql"])
        elif kind == "show":
            cell_id = agent.add_sql_cell(notebook_id, command=spec["command"], table=spec.get("table", ""))
        else:
            raise ValueError(f"unknown cell kind {kind!r}")
        async for _ in agent.run_cell(notebook_id, cell_id):
            pass


async def run_ai_cell(agent: Agent, notebook_id: str, prompt: str, max_rows: int) -> tuple[str, AsyncIterator[Event]]:
    """Add an AI Cell at the end of the notebook and start running it: (its id, its events)."""
    cell_id = agent.add_ai_cell(notebook_id, prompt, max_rows=max_rows)
    return cell_id, agent.run_cell(notebook_id, cell_id)
