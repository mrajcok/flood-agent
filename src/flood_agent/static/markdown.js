// Render the LLM's reply text as Markdown, sanitised (the text comes from the model).
//
// The server sends reply text as <span class="md-delta">escaped text</span>: one per streamed chunk in a
// live Run, one per message when a notebook is reopened. Each delta is folded into a segment
// (<div class="md">) that keeps the raw source in data-src and is re-rendered as a whole, so Markdown
// split across chunks (`**fix` + `es**`) renders once the closing part arrives. A Display between two
// deltas ends the segment, so text after a table or chart starts a new one below it.
(function () {
  const pending = new Set();

  function render(seg) {
    const src = seg.dataset.src || "";
    if (window.marked && window.DOMPurify) {
      seg.innerHTML = DOMPurify.sanitize(marked.parse(src, { gfm: true, breaks: true }));
    } else {  // the libraries did not load: show the text as it was written
      seg.classList.add("md-plain");
      seg.textContent = src;
    }
  }

  function flush() {
    for (const seg of pending) render(seg);
    pending.clear();
  }

  function absorb(delta) {
    if (!delta.isConnected) return;
    let seg = delta.previousElementSibling;
    if (!seg || !seg.classList.contains("md")) {
      seg = document.createElement("div");
      seg.className = "md";
      seg.dataset.src = "";
      delta.before(seg);
    }
    seg.dataset.src += delta.textContent;
    delta.remove();
    if (!pending.size) requestAnimationFrame(flush);  // at most one render per segment per frame
    pending.add(seg);
  }

  function absorbWithin(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches(".md-delta")) absorb(node);
    else node.querySelectorAll(".md-delta").forEach(absorb);
  }

  function start() {
    absorbWithin(document.body);
    new MutationObserver(records => {
      for (const r of records) r.addedNodes.forEach(absorbWithin);
    }).observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
