// Wires up every .hover-diagram on the page, as the Cirro data governance diagram does.
// A part marked data-key="x" shows the note data-note="x" and dims every [data-r] whose
// list lacks x; data-note="" is the note shown when nothing is selected.
(() => {
  const SWITCH_DELAY_MS = 120;
  const LEAVE_DELAY_MS = 350;
  for (const root of document.querySelectorAll(".hover-diagram:not([data-ready])")) {
    root.dataset.ready = "";
    const svg = root.querySelector("svg");
    let current = null;
    let pending;
    const show = (key) => {
      clearTimeout(pending);
      current = key;
      for (const el of root.querySelectorAll("[data-r]")) {
        el.classList.toggle("hd-dim", Boolean(key) && !el.dataset.r.split(" ").includes(key));
      }
      for (const note of root.querySelectorAll("[data-note]")) {
        note.classList.toggle("is-shown", note.dataset.note === (key || ""));
      }
    };
    // A new state only takes over once the pointer has rested on it, so brushing
    // across a boundary and back never changes anything.
    const showSoon = (key, delay) => {
      clearTimeout(pending);
      if (key !== current) pending = setTimeout(() => show(key), delay);
    };
    const keyOf = (target) => target.closest("[data-key]")?.dataset.key ?? null;
    // Gaps between areas keep the current selection rather than resetting it.
    svg.addEventListener("pointerover", (e) => showSoon(keyOf(e.target) ?? current, SWITCH_DELAY_MS));
    svg.addEventListener("pointerleave", () => showSoon(svg.contains(document.activeElement) ? keyOf(document.activeElement) : null, LEAVE_DELAY_MS));
    svg.addEventListener("focusin", (e) => show(keyOf(e.target)));
    svg.addEventListener("focusout", (e) => { if (!svg.contains(e.relatedTarget)) show(null); });
    svg.addEventListener("keydown", (e) => { if (e.key === "Escape") document.activeElement.blur(); });
  }
})();
