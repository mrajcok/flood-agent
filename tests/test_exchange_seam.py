"""The Agent runs an LLM exchange from a given history and table list, recording against any owner."""

from conftest import ScriptedLLM, tool_call

from flood_agent.agent import TraceReady, _TurnQueue
from flood_agent.catalog import CellRun
from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


class CellSink:
    """What an AI Cell's Run will supply: its own owner and storage."""

    owner = CellRun("cell-1", "run-1")
    key = "cell-1"
    session_id = "cell-cell-1"

    def __init__(self):
        self.recorded, self.steps, self.traces = [], [], []

    def record(self, history, message):
        history.append(message)
        self.recorded.append(message)
        return 0

    def add_step(self, items):
        self.steps.append(items)
        return [0] * len(items), [[0] * len(d) for _, d in items]

    def save_trace(self, trace):
        self.traces.append(trace)


async def test_an_exchange_runs_from_a_given_history_and_records_against_a_cell_run(make_agent):
    llm = ScriptedLLM([TextChunk("Looking."), tool_call("run_query", sql=SQL, title="Ports")], [TextChunk("Done.")])
    agent = await make_agent(llm)
    sink, queue = CellSink(), _TurnQueue()
    history = [{"role": "user", "content": "[Markdown note] hi"}, {"role": "user", "content": "ports please"}]
    asked = []

    await agent._exchange(history, lambda: asked.append(1) or [], sink, "ports please", queue, 1000,
                          agent.config.model, None, end=lambda trace: TraceReady(trace))

    assert [m["role"] for m in history] == ["user", "user", "assistant", "tool", "assistant"]
    assert len(sink.steps) == 2 and len(sink.traces) == 1
    assert len(asked) == 2  # the table list is asked before every LLM step
    [table] = agent.tables.tables_of_run("cell-1", "run-1")
    assert table.owner == CellRun("cell-1", "run-1")
