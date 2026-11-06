// Turn the LLM's Chart Spec into the spec the page embeds: sized to fit, with tooltips.
//
// Tooltips are on by default: hovering a mark shows its encoded fields; a spec's own tooltip wins.
// Numbers get thousands separators and at most 1 decimal, and times show to the second (Vega-Lite's
// default is the date only), in UTC on a utc scale.
//
// A line is thin, so hovering exactly on it is fiddly. A single-view line chart gets an invisible
// point per row with a `nearest` selection: Vega-Lite covers the plot with a Voronoi layer, so the
// pointer anywhere in the plot shows the closest point's tooltip. (`nearest` does not work on line
// marks themselves, hence the extra layer. Areas are left alone: they stack by default and the
// points would not.)
//
// A chart with a time x axis zooms on that axis: Ctrl+scroll (Cmd on a Mac, or a trackpad pinch) zooms
// at the pointer, dragging pans, double-click resets. A plain scroll still scrolls the page. Not
// Shift+scroll: Chrome on Windows and Linux sends it as a horizontal scroll, which the zoom ignores.
// Marks are clipped so a zoomed line stays inside the plot.
const ZOOM = {
  name: "zoom",
  select: { type: "interval", encodings: ["x"], zoom: "wheel![event.ctrlKey || event.metaKey]" },
  bind: "scales",
};

window.chartIsZoomable = function (spec) {
  return spec.encoding?.x?.type === "temporal" && !spec.params && !spec.layer;
};

window.chartEmbedSpec = function (spec) {
  const config = {
    tooltipFormat: { numberFormat: ",.1~f", timeFormat: "%Y-%m-%d %H:%M:%S" },
    ...spec.config,
    mark: { tooltip: true, ...spec.config?.mark },
  };
  let out = { ...spec, config, width: "container", autosize: { type: "fit-x", contains: "padding" } };
  const markType = typeof spec.mark === "string" ? spec.mark : spec.mark?.type;
  const zoomable = chartIsZoomable(spec);
  if (zoomable) {
    out.mark = { ...(typeof spec.mark === "string" ? { type: spec.mark } : spec.mark), clip: true };
    out.params = [ZOOM];
  }
  if (markType === "line" && !spec.params) {
    const { mark, params, ...rest } = out;
    const color = spec.encoding?.color;
    // One series: snap on x alone. Nearest in 2D picks the wrong time on a steep line, where a point
    // hours away can be closer than the one under the pointer. Several series share each x, so there
    // 2D nearest stays: moving up and down picks the series.
    const oneSeries = !color && !spec.encoding?.detail;
    out = {
      ...rest,
      // Keep the legend's line symbols: the hidden points would turn them into circles.
      ...(color && color.legend !== null && {
        encoding: { ...spec.encoding, color: { ...color, legend: { symbolType: "stroke", ...color.legend } } },
      }),
      layer: [
        { mark, ...(params && { params }) },
        {
          mark: { type: "point", opacity: 0, ...(zoomable && { clip: true }) },
          params: [{ name: "hover", select: { type: "point", on: "pointerover", nearest: true, clear: "pointerout",
                                               ...(oneSeries && { encodings: ["x"] }) } }],
        },
      ],
    };
  }
  return out;
};
