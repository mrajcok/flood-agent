"""Saved Conversations: persistence through the Agent turn seam and the thin HTTP layer."""

import re

from fastapi.testclient import TestClient

from flood_defender.llm import TextChunk
from flood_defender.web import create_app

from conftest import ScriptedLLM, last_tool_result, tool_call

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def table_script(reply="Here you go."):
    return [
        [tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk(reply)],
    ]


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id})
    assert posted.status_code == 200
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def main_pane(page: str) -> str:
    """The page minus the sidebar."""
    return page.split("</aside>")[1]


def test_conversation_survives_restart_and_is_listed_in_sidebar(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM(*table_script()))))
    cid, _ = ask(client, "top ports by bytes")

    restarted = TestClient(create_app(make_agent(ScriptedLLM())))  # new app, same files
    home = restarted.get("/").text
    assert f'href="/c/{cid}"' in home and "top ports by bytes" in home

    reopened = restarted.get(f"/c/{cid}").text
    assert "top ports by bytes" in main_pane(reopened) and "Here you go." in reopened


def test_reopening_rerenders_table_display_from_its_result_table(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM(*table_script()))))
    cid, _ = ask(client, "top ports")

    restarted = TestClient(create_app(make_agent(ScriptedLLM())))
    page = restarted.get(f"/c/{cid}").text
    rows_url = page.split('data-rows-url="')[1].split('"')[0]
    assert restarted.get(rows_url).json()["rows"] == [[8443, 1_111_111], [9053, 555_321]]


def test_follow_up_in_reopened_conversation_sees_prior_history(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM(*table_script()))))
    cid, _ = ask(client, "first question")

    second = ScriptedLLM([TextChunk("ok")])
    restarted = TestClient(create_app(make_agent(second)))
    ask(restarted, "second question", cid)
    sent = second.all_sent_text()
    assert "first question" in sent and "second question" in sent


def test_new_conversation_starts_clean_and_is_separate(make_agent):
    llm = ScriptedLLM(*table_script("first reply"), [TextChunk("second reply")])
    client = TestClient(create_app(make_agent(llm)))
    cid1, _ = ask(client, "alpha question")
    cid2, _ = ask(client, "beta question", "")  # empty id = new Conversation
    assert cid1 != cid2

    assert not any("alpha question" in str(m.get("content")) for m in llm.calls[-1])

    page2 = main_pane(client.get(f"/c/{cid2}").text)
    assert "beta question" in page2 and "alpha question" not in page2
    assert "data-rows-url" not in page2
    home = client.get("/").text
    assert f"/c/{cid1}" in home and f"/c/{cid2}" in home
    assert "alpha question" not in main_pane(home)


def test_result_tables_are_not_mixed_between_conversations(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM(*table_script(), *table_script()))))
    cid1, body1 = ask(client, "q one")
    cid2, body2 = ask(client, "q two")
    t1 = body1.split('data-rows-url="')[1].split('"')[0]
    t2 = body2.split('data-rows-url="')[1].split('"')[0]
    assert t1 != t2
    page1, page2 = client.get(f"/c/{cid1}").text, client.get(f"/c/{cid2}").text
    assert t1 in page1 and t2 not in page1
    assert t2 in page2 and t1 not in page2


def test_unknown_conversation_is_404(make_agent):
    client = TestClient(create_app(make_agent(ScriptedLLM())))
    assert client.get("/c/deadbeef").status_code == 404


async def test_agent_cannot_display_another_conversations_result_table(make_agent):
    from conftest import collect
    from flood_defender.agent import Conversation, DisplayEvent

    agent = make_agent(ScriptedLLM(*table_script()))
    first = Conversation()
    events = await collect(agent, "q", first)
    table = next(e.result_table for e in events if isinstance(e, DisplayEvent))

    intruder = ScriptedLLM([tool_call("show_table", result_table=table)], [TextChunk("x")])
    other = make_agent(intruder)
    events = await collect(other, "steal", Conversation())
    assert not [e for e in events if isinstance(e, DisplayEvent)]

    reopened = other.open_conversation(first.id)
    assert [m["role"] for m in reopened.messages][0] == "user" and len(reopened.messages) == len(first.messages)
