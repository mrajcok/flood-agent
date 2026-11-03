"""Saved Conversations: persistence through the Agent turn seam and the thin HTTP layer."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call

from flood_agent.llm import TextChunk

SQL = "SELECT dport, SUM(bytes) AS total_bytes FROM flows GROUP BY dport ORDER BY total_bytes DESC"


def table_script(reply="Here you go."):
    return [
        [tool_call("run_query", sql=SQL, title="t")],
        lambda msgs: [tool_call("show_table", result_table=last_tool_result(msgs)["result_table"])],
        [TextChunk(reply)],
    ]


def ask(client, message, conversation_id=""):
    posted = client.post("/chat", data={"message": message, "conversation_id": conversation_id, "max_rows": "1000"})
    assert posted.status_code == 200
    cid = re.search(r'name="conversation_id" value="([0-9a-f]+)"', posted.text).group(1)
    stream_url = posted.text.split('sse-connect="')[1].split('"')[0]
    return cid, client.get(stream_url).text


def main_pane(page: str) -> str:
    """The page minus the sidebar."""
    return page.split("</aside>")[1]


def test_conversation_survives_restart_and_is_listed_in_sidebar(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script())))
    cid, _ = ask(client, "top ports by bytes")

    restarted = web_client(build_agent(ScriptedLLM()))  # new app, same files
    home = restarted.get("/").text
    assert f'href="/c/{cid}"' in home and "Conversation 1" in home

    reopened = restarted.get(f"/c/{cid}").text
    assert "top ports by bytes" in main_pane(reopened) and "Here you go." in reopened


def test_reopening_rerenders_table_display_from_its_result_table(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script())))
    cid, _ = ask(client, "top ports")

    restarted = web_client(build_agent(ScriptedLLM()))
    page = restarted.get(f"/c/{cid}").text
    rows_url = page.split('data-rows-url="')[1].split('"')[0]
    assert restarted.get(rows_url).json()["rows"] == [[8443, 1_111_111], [9053, 555_321]]


def test_follow_up_in_reopened_conversation_sees_prior_history(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script())))
    cid, _ = ask(client, "first question")

    second = ScriptedLLM([TextChunk("ok")])
    restarted = web_client(build_agent(second))
    ask(restarted, "second question", cid)
    sent = second.all_sent_text()
    assert "first question" in sent and "second question" in sent


def test_new_conversation_starts_clean_and_is_separate(build_agent, web_client):
    llm = ScriptedLLM(*table_script("first reply"), [TextChunk("second reply")])
    client = web_client(build_agent(llm))
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


def test_result_tables_are_not_mixed_between_conversations(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script(), *table_script())))
    cid1, body1 = ask(client, "q one")
    cid2, body2 = ask(client, "q two")
    t1 = body1.split('data-rows-url="')[1].split('"')[0]
    t2 = body2.split('data-rows-url="')[1].split('"')[0]
    assert t1 != t2
    page1, page2 = client.get(f"/c/{cid1}").text, client.get(f"/c/{cid2}").text
    assert t1 in page1 and t2 not in page1
    assert t2 in page2 and t1 not in page2


def test_unknown_conversation_is_404(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.get("/c/deadbeef").status_code == 404


def test_another_conversation_can_display_a_table_and_deleting_it_leaves_the_table(build_agent, web_client):
    """ADR 0003: Conversation B may show A's Result Table; the Display is saved with B, but A still
    owns the table, so deleting B (even with delete_all) does not drop it."""
    client = web_client(build_agent(ScriptedLLM(
        *table_script(),
        lambda msgs: [tool_call("show_table", result_table=msgs[-1]["content"].split()[-1])],
        [TextChunk("there it is")],
    )))
    a, _ = ask(client, "top ports")
    table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{a}").text).group(1)

    b, body = ask(client, f"show {table}")
    assert f"/api/results/{table}/rows" in body
    assert f"/api/results/{table}/rows" in client.get(f"/c/{b}").text  # the Display is saved with B

    assert client.get(f"/c/{b}/delete-preview").json()["total"] == 0
    client.post(f"/c/{b}/delete", data={"tables": "delete_all"})
    assert client.get(f"/api/results/{table}/rows").status_code == 200
    assert f"/api/results/{table}/rows" in client.get(f"/c/{a}").text



def test_deleting_a_conversation_removes_it_and_its_result_tables(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script(), [TextChunk("other")])))
    cid, body = ask(client, "top ports")
    other, _ = ask(client, "something else")
    table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{cid}").text).group(1)
    assert client.get(f"/api/results/{table}/rows").status_code == 200

    deleted = client.post(f"/c/{cid}/delete", data={"tables": "delete_all"}, follow_redirects=False)
    assert deleted.status_code == 303 and deleted.headers["location"] == "/"

    assert client.get(f"/c/{cid}").status_code == 404
    home = client.get("/").text
    assert f'href="/c/{cid}"' not in home and f'href="/c/{other}"' in home
    assert client.get(f"/api/results/{table}/rows").status_code != 200
    assert client.get(f"/c/{other}").status_code == 200

    restarted = web_client(build_agent(ScriptedLLM()))
    assert f'href="/c/{cid}"' not in restarted.get("/").text


def test_deleting_an_unknown_conversation_is_404(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    assert client.post("/c/nope/delete").status_code == 404


def sidebar_titles(client):
    return re.findall(r'<a href="/c/[0-9a-f]+"[^>]*>([^<]*)</a>', client.get("/").text)


def test_conversations_are_named_conversation_1_conversation_2_and_numbers_are_not_reused(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")], [TextChunk("c")])))
    first, _ = ask(client, "one")
    ask(client, "two")
    assert sorted(sidebar_titles(client)) == ["Conversation 1", "Conversation 2"]

    client.post(f"/c/{first}/delete")
    ask(client, "three")
    assert sorted(sidebar_titles(client)) == ["Conversation 2", "Conversation 3"]


def test_a_conversation_can_be_renamed(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")])))
    cid, _ = ask(client, "one")

    renamed = client.post(f"/c/{cid}/rename", data={"title": "  SYN flood <b>probe</b> "}, follow_redirects=False)

    assert renamed.status_code == 303 and renamed.headers["location"] == f"/c/{cid}"
    assert sidebar_titles(client) == ["SYN flood &lt;b&gt;probe&lt;/b&gt;"]
    restarted = web_client(build_agent(ScriptedLLM()))
    assert sidebar_titles(restarted) == ["SYN flood &lt;b&gt;probe&lt;/b&gt;"]


def test_rename_rejects_a_blank_title_and_an_unknown_conversation(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")])))
    cid, _ = ask(client, "one")
    assert client.post(f"/c/{cid}/rename", data={"title": "   "}).status_code == 422
    assert sidebar_titles(client) == ["Conversation 1"]
    assert client.post("/c/nope/rename", data={"title": "x"}).status_code == 404


def test_each_conversation_has_a_menu_offering_rename_and_delete(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    cid, _ = ask(client, "hello")
    page = client.get("/").text
    assert 'aria-label="Conversation options"' in page
    assert f'action="/c/{cid}/rename"' in page and "Rename" in page
    assert f'action="/c/{cid}/delete"' in page and "Delete" in page


def test_rename_edits_the_name_in_a_textbox_prefilled_with_the_current_name(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    cid, _ = ask(client, "hello")
    client.post(f"/c/{cid}/rename", data={"title": 'Say "hi"'})
    page = client.get("/").text
    form = page.split(f'action="/c/{cid}/rename"')[1].split("</form>")[0]
    assert '<input' in form and 'name="title"' in form and 'type="text"' in form
    assert 'value="Say &#34;hi&#34;"' in form
    assert "prompt(" not in page


def test_sidebar_offers_a_delete_control_per_conversation(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    cid, _ = ask(client, "hello")
    assert f'action="/c/{cid}/delete"' in client.get("/").text


def test_a_submitted_prompt_shows_a_thinking_spinner_until_the_first_event_arrives(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    posted = client.post("/chat", data={"message": "hello", "conversation_id": "", "max_rows": "1000"}).text
    holder = re.search(r'<div sse-swap="([^"]*)" hx-swap="delete">\s*<span class="thinking" role="status"', posted)
    assert holder, "spinner removes itself when it receives an event"
    # whichever stream event arrives first removes it, including a stream that only ends
    assert set(holder[1].split(",")) == {"text", "flag", "display", "trace", "end"}


def test_each_prompt_and_its_response_collapse_under_the_prompt(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("reply-alpha")], [TextChunk("reply-beta")])))
    cid, _ = ask(client, "first question")
    live = client.post("/chat", data={"message": "second question", "conversation_id": cid, "max_rows": "1000"}).text
    ask_stream = live.split('sse-connect="')[1].split('"')[0]
    client.get(ask_stream)
    assert 'class="turn" open' in live and '<summary class="user"><span>second question</span>' in live

    page = main_pane(client.get(f"/c/{cid}").text)
    turns = page.split('<details class="turn" open>')[1:]
    assert len(turns) == 2
    assert turns[0].startswith('<summary class="user"><span>first question</span>') and "reply-alpha" in turns[0]
    assert "reply-beta" not in turns[0] and "second question" not in turns[0]
    assert turns[1].startswith('<summary class="user"><span>second question</span>') and "reply-beta" in turns[1]


def test_the_open_conversation_is_highlighted_in_the_sidebar(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")])))
    first, _ = ask(client, "one")
    second, _ = ask(client, "two")

    def current(page):
        return re.findall(r'<li[^>]*class="current"[^>]*>\s*<a href="/c/([0-9a-f]+)"', page)

    assert current(client.get(f"/c/{first}").text) == [first]
    assert current(client.get(f"/c/{second}").text) == [second]
    assert current(client.get("/").text) == []
    # the sidebar sent after a prompt is submitted marks that session too
    posted = client.post("/chat", data={"message": "three", "conversation_id": first, "max_rows": "1000"}).text
    assert current(posted) == [first]


def turn_ids(page: str) -> list[str]:
    return re.findall(r'action="/c/[0-9a-f]+/turns/(\d+)/delete"', page)


def test_deleting_a_turn_removes_its_prompt_answer_and_result_tables_only(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM(*table_script("reply-alpha"), [TextChunk("reply-beta")])))
    cid, _ = ask(client, "first question")
    ask(client, "second question", cid)
    table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{cid}").text).group(1)
    first, second = turn_ids(main_pane(client.get(f"/c/{cid}").text))

    deleted = client.post(f"/c/{cid}/turns/{first}/delete", data={"tables": "delete_all"}, follow_redirects=False)

    assert deleted.status_code == 303 and deleted.headers["location"] == f"/c/{cid}"
    page = main_pane(client.get(f"/c/{cid}").text)
    assert "<span>first question</span>" not in page and "reply-alpha" not in page
    assert "<span>second question</span>" in page and "reply-beta" in page
    assert turn_ids(page) == [second]
    assert client.get(f"/api/results/{table}/rows").status_code != 200


def test_deleting_a_later_turn_leaves_an_earlier_turns_result_table_readable(
    build_agent, web_client
):
    later = [[tool_call("run_query", sql="SELECT dport FROM flows", title="t")],
               [TextChunk("done")]]
    client = web_client(build_agent(ScriptedLLM(*table_script("one"), *later)))
    cid, _ = ask(client, "first question")
    ask(client, "second question", cid)
    first, second = turn_ids(main_pane(client.get(f"/c/{cid}").text))
    first_table = re.search(r"/api/results/([a-z0-9_]+)/rows", client.get(f"/c/{cid}").text).group(1)

    client.post(f"/c/{cid}/turns/{second}/delete")

    assert client.get(f"/api/results/{first_table}/rows").status_code == 200  # created by the first turn


def test_deleting_a_turn_rejects_unknown_and_foreign_turns(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("a")], [TextChunk("b")])))
    one, _ = ask(client, "one")
    two, _ = ask(client, "two")
    (turn_in_one,) = turn_ids(client.get(f"/c/{one}").text)
    assert client.post(f"/c/{one}/turns/9999/delete").status_code == 404
    assert client.post(f"/c/{two}/turns/{turn_in_one}/delete").status_code == 404
    assert client.post("/c/nope/turns/1/delete").status_code == 404
    assert "one" in client.get(f"/c/{one}").text


def test_each_prompt_has_an_x_with_a_confirmation_both_live_and_on_reopen(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM([TextChunk("hi")])))
    cid, body = ask(client, "hello")
    assert "event: turn" in body and "/turns/" in body  # the live turn gets its x once its id is known

    page = main_pane(client.get(f"/c/{cid}").text)
    summary = page.split("<summary")[1].split("</summary>")[0]
    assert "hello" in summary and 'action="/c/' in summary and "&times;" in summary
    assert "data-preview-url" in summary and "data-confirm" in summary


def test_sidebar_button_reads_new_chat_and_no_text_says_session(build_agent, web_client):
    client = web_client(build_agent(ScriptedLLM()))
    ask(client, "hi")
    page = client.get("/").text
    assert re.search(r'class="new"[^>]*>New chat<', page)
    assert "New conversation" not in page
    assert "session" not in re.sub(r"<script.*?</script>", "", page, flags=re.S).lower()
