"""Hovering a chart mark shows a tooltip with the mark's encoded fields."""

import re

from conftest import ScriptedLLM, last_tool_result, tool_call
from playwright.sync_api import expect

from flood_agent.llm import TextChunk


def chart_script(sql, mark, encoding):
    return [
        [tool_call("run_query", sql=sql, title="t")],
        lambda msgs: [
            tool_call(
                "show_chart",
                vega_lite_spec={
                    "data": {"name": last_tool_result(msgs)["result_table"]},
                    "mark": mark,
                    "encoding": encoding,
                },
            )
        ],
        [TextChunk("Here is the chart.")],
    ]


def tooltip_on_hover(page):
    """Ask for the scripted chart, then sweep the pointer over the plot until a tooltip shows."""
    page.get_by_placeholder("Write a prompt").fill("chart it")
    page.get_by_role("button", name="Send message (Enter)").click()
    plot = page.locator(".display.chart canvas, .display.chart svg.marks").first
    expect(plot).to_be_visible()
    expect(page.locator(".live-trace")).to_have_count(0)  # the layout stops moving once the live trace has folded away
    tooltip = page.locator("#vg-tooltip-element.visible")
    box = plot.bounding_box()
    assert box
    for i in range(1, 40):
        for j in range(1, 10):
            page.mouse.move(box["x"] + box["width"] * i / 40, box["y"] + box["height"] * j / 10)
            if tooltip.count():
                return tooltip
    raise AssertionError("no tooltip anywhere on the plot")


def test_hovering_a_bar_shows_its_fields_in_a_tooltip_with_thousands_separators(live_page, build_agent):
    script = chart_script(
        "SELECT dport, bytes FROM flows WHERE bytes = 987654",
        "bar",
        {
            "x": {"field": "dport", "type": "nominal", "title": "Port"},
            "y": {"field": "bytes", "type": "quantitative", "title": "Bytes"},
        },
    )
    tooltip = tooltip_on_hover(live_page(agent=build_agent(ScriptedLLM(*script))))
    for text in ("Port", "8443", "Bytes", "987,654"):
        expect(tooltip).to_contain_text(text)


def test_a_time_series_tooltip_shows_the_utc_time_to_the_second_in_any_browser_timezone(live_page, build_agent):
    script = chart_script(
        "SELECT stime, bytes FROM flows WHERE bytes = 987654",
        {"type": "point", "size": 2000},
        {
            "x": {"field": "stime", "type": "temporal", "scale": {"type": "utc"}, "title": "Time (UTC)"},
            "y": {"field": "bytes", "type": "quantitative", "title": "Bytes"},
        },
    )
    page = live_page(agent=build_agent(ScriptedLLM(*script)), timezone="America/New_York")
    expect(tooltip_on_hover(page)).to_contain_text("2023-11-14 22:13:20")  # stime 1_700_000_000_111


def test_a_line_chart_shows_the_nearest_points_tooltip_with_the_pointer_anywhere_in_the_plot(live_page, build_agent):
    script = chart_script(
        "SELECT stime, bytes FROM flows ORDER BY stime",
        "line",
        {
            "x": {"field": "stime", "type": "temporal", "scale": {"type": "utc"}, "title": "Time (UTC)"},
            "y": {"field": "bytes", "type": "quantitative", "title": "Bytes"},
        },
    )
    page = live_page(agent=build_agent(ScriptedLLM(*script)))
    page.get_by_placeholder("Write a prompt").fill("chart it")
    page.get_by_role("button", name="Send message (Enter)").click()
    plot = page.locator(".display.chart canvas, .display.chart svg.marks").first
    expect(plot).to_be_visible()
    # The line runs from 987,654 bytes down to 123,457 and back up to 555,321: the plot's top-right
    # corner is far from it, and the nearest point is the last one.
    origin = page.locator(".display.chart").evaluate(
        "el => { const v = Alpine.$data(el).view; return [...v.origin(), v.width()]; }"
    )
    box = plot.bounding_box()
    assert box
    page.mouse.move(box["x"] + origin[0] + origin[2] - 5, box["y"] + origin[1] + 5)
    tooltip = page.locator("#vg-tooltip-element.visible")
    expect(tooltip).to_contain_text("555,321")
    expect(tooltip).to_contain_text("2023-11-14 22:13:22")


def embed_steep_line(page):
    """Embed a steep hourly curve over six days in the live page; return its canvas."""
    page.evaluate("""async () => {
        const t0 = Date.UTC(2026, 0, 1);
        const values = Array.from({ length: 144 }, (_, h) =>
            ({ t: t0 + h * 3600e3, v: 150e6 + 90e6 * Math.sin((h - 8) / 24 * 2 * Math.PI) }));
        const el = document.createElement('div');
        el.id = 'steep';
        el.style.width = '800px';
        document.body.prepend(el);
        const spec = { data: { values }, mark: 'line', encoding: {
            x: { field: 't', type: 'temporal', scale: { type: 'utc' } },
            y: { field: 'v', type: 'quantitative' } } };
        window.steep = (await vegaEmbed(el, chartEmbedSpec(spec), { actions: false })).view;
    }""")
    return page.locator("#steep canvas")


def test_a_steep_line_shows_the_time_under_the_pointer_at_any_height(live_page):
    """2D nearest picks a point hours away on a steep line (it can be nearer than the one below the
    pointer), so one series snaps on x alone."""
    page = live_page()
    box = embed_steep_line(page).bounding_box()
    assert box
    noon = page.evaluate("""() => { const v = window.steep, p = v.padding();
        return [p.left + v.origin()[0] + v.scale('x')(Date.UTC(2026, 0, 2, 12)), p.top + v.origin()[1], v.height()]; }""")
    tooltip = page.locator("#vg-tooltip-element.visible")
    for frac in (0.05, 0.5, 0.95):
        page.mouse.move(box["x"] + noon[0], box["y"] + noon[1] + noon[2] * frac)
        expect(tooltip).to_contain_text("2026-01-02 12:00:00")
    expect(tooltip).to_contain_text(re.compile(r"\d{3},\d{3},\d{3}\.\d(?!\d)"))  # at most 1 decimal


def test_ctrl_scroll_zooms_a_time_chart_and_double_click_resets_it_but_a_plain_scroll_does_not(live_page):
    page = live_page()
    canvas = embed_steep_line(page)
    span = lambda: page.evaluate("() => { const d = window.steep.scale('x').domain(); return d[1] - d[0]; }")  # noqa: E731
    full = span()
    canvas.hover()
    page.mouse.wheel(0, -300)
    assert span() == full
    page.keyboard.down("Control")
    page.mouse.wheel(0, -300)
    page.keyboard.up("Control")
    page.wait_for_function(f"() => {{ const d = window.steep.scale('x').domain(); return d[1] - d[0] < {full}; }}")
    canvas.dblclick()
    page.wait_for_function(f"() => {{ const d = window.steep.scale('x').domain(); return d[1] - d[0] === {full}; }}")


def test_only_charts_with_a_time_axis_show_the_zoom_hint(live_page, build_agent):
    time_chart = chart_script(
        "SELECT stime, bytes FROM flows ORDER BY stime",
        "line",
        {"x": {"field": "stime", "type": "temporal"}, "y": {"field": "bytes", "type": "quantitative"}},
    )
    bar_chart = chart_script(
        "SELECT dport, bytes FROM flows",
        "bar",
        {"x": {"field": "dport", "type": "nominal"}, "y": {"field": "bytes", "type": "quantitative"}},
    )
    for script, shown in ((bar_chart, False), (time_chart, True)):
        page = live_page(agent=build_agent(ScriptedLLM(*script)))
        page.get_by_placeholder("Write a prompt").fill("chart it")
        page.get_by_role("button", name="Send message (Enter)").click()
        expect(page.locator(".display.chart canvas")).to_be_visible()
        hint = page.get_by_text("Ctrl+scroll to zoom, drag to pan, double-click to reset")
        expect(hint).to_be_visible() if shown else expect(hint).to_be_hidden()
    expect(page.locator(".live-trace")).to_have_count(0)  # the layout stops moving once the live trace has folded away
    # The hint sits just under the chart, at its right edge.
    chart, note = page.locator(".display.chart canvas").bounding_box(), page.locator(".chart-hint").bounding_box()
    assert chart and note
    assert 0 <= note["y"] - (chart["y"] + chart["height"]) < 10
    assert page.locator(".chart-hint").evaluate("el => getComputedStyle(el).textAlign") == "right"
