"""Interactive knowledge graph view: an inline CCv2 component drawing a force-directed SVG graph.

Self-contained (no CDN libraries). Drag nodes, drag the background to pan, scroll to zoom, click a node to select it.
The selected node id comes back to Python as the `selected` trigger.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import streamlit as st

ENTITY_COLORS = {
    "person": "#6366F1",
    "project": "#0EA5E9",
    "meeting": "#64748B",
    "decision": "#10B981",
    "action_item": "#F59E0B",
    "blocker": "#EF4444",
    "technology": "#8B5CF6",
    "topic": "#14B8A6",
    "organization": "#EC4899",
}

ENTITY_LABELS = {
    "person": "People",
    "project": "Projects",
    "meeting": "Meetings",
    "decision": "Decisions",
    "action_item": "Action items",
    "blocker": "Blockers",
    "technology": "Technologies",
    "topic": "Topics",
    "organization": "Organizations",
}

_HTML = """
<div class="kg-wrap">
  <svg class="kg-svg"><g class="kg-view"><g class="kg-edges"></g><g class="kg-nodes"></g></g></svg>
  <div class="kg-hint">Drag to pan · scroll to zoom · click a node for details</div>
</div>
"""

_CSS = """
.kg-wrap { position: relative; width: 100%; height: 620px; border: 1px solid var(--st-border-color);
  border-radius: var(--st-base-radius); background: var(--st-secondary-background-color); overflow: hidden; }
.kg-svg { width: 100%; height: 100%; cursor: grab; user-select: none; }
.kg-svg.panning { cursor: grabbing; }
.kg-edge { stroke: var(--st-text-color); stroke-opacity: 0.18; }
.kg-edge.hot { stroke: var(--st-primary-color); stroke-opacity: 0.85; }
.kg-edge-label { font: 10px var(--st-font); fill: var(--st-text-color); opacity: 0; pointer-events: none; }
.kg-edge-label.hot { opacity: 0.8; }
.kg-node { cursor: pointer; }
.kg-node circle { stroke: var(--st-background-color); stroke-width: 2; }
.kg-node text { font: 11px var(--st-font); fill: var(--st-text-color); pointer-events: none; }
.kg-node.dim { opacity: 0.25; }
.kg-node.selected circle { stroke: var(--st-primary-color); stroke-width: 3.5; }
.kg-hint { position: absolute; left: 10px; bottom: 8px; font: 11px var(--st-font); color: var(--st-text-color);
  opacity: 0.55; pointer-events: none; }
"""

_JS = """
const SVG_NS = "http://www.w3.org/2000/svg";
const instances = new WeakMap();

function el(tag, attrs, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function build(root, data, setTriggerValue) {
  const svg = root.querySelector(".kg-svg");
  const view = root.querySelector(".kg-view");
  const edgeLayer = root.querySelector(".kg-edges");
  const nodeLayer = root.querySelector(".kg-nodes");
  edgeLayer.replaceChildren();
  nodeLayer.replaceChildren();

  const width = svg.clientWidth || 900, height = svg.clientHeight || 620;
  const colors = data.colors || {};
  const nodes = (data.nodes || []).map((n, i) => {
    const angle = (i / Math.max(1, data.nodes.length)) * Math.PI * 2;
    const r = 120 + (i % 7) * 25;
    return { ...n, x: width / 2 + Math.cos(angle) * r, y: height / 2 + Math.sin(angle) * r, vx: 0, vy: 0,
             radius: 6 + Math.min(14, Math.sqrt(n.meetings || 1) * 4) };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = (data.edges || []).filter((e) => byId.has(e.source) && byId.has(e.target))
    .map((e) => ({ ...e, s: byId.get(e.source), t: byId.get(e.target) }));
  const neighbours = new Map(nodes.map((n) => [n.id, new Set([n.id])]));
  edges.forEach((e) => { neighbours.get(e.source).add(e.target); neighbours.get(e.target).add(e.source); });

  edges.forEach((e) => {
    e.line = el("line", { class: "kg-edge", "stroke-width": Math.min(4, 1 + Math.log2(e.weight || 1)) }, edgeLayer);
    e.label = el("text", { class: "kg-edge-label", "text-anchor": "middle" }, edgeLayer);
    e.label.textContent = (e.type || "").replace(/_/g, " ");
  });
  nodes.forEach((n) => {
    n.g = el("g", { class: "kg-node" }, nodeLayer);
    el("circle", { r: n.radius, fill: colors[n.type] || "#94A3B8" }, n.g);
    const label = el("text", { x: n.radius + 4, y: 4 }, n.g);
    label.textContent = n.label.length > 32 ? n.label.slice(0, 31) + "…" : n.label;
    const title = el("title", {}, n.g);
    title.textContent = `${n.label} (${(n.type || "").replace(/_/g, " ")})` + (n.description ? `\\n${n.description}` : "");
  });

  // Pan / zoom
  const state = { k: 1, tx: 0, ty: 0, alpha: 1, dragging: null, panning: null, moved: false };
  const applyView = () => view.setAttribute("transform", `translate(${state.tx},${state.ty}) scale(${state.k})`);
  const toGraph = (evt) => {
    const rect = svg.getBoundingClientRect();
    return { x: (evt.clientX - rect.left - state.tx) / state.k, y: (evt.clientY - rect.top - state.ty) / state.k };
  };
  svg.onwheel = (evt) => {
    evt.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = evt.clientX - rect.left, my = evt.clientY - rect.top;
    const k = Math.min(4, Math.max(0.2, state.k * (evt.deltaY < 0 ? 1.12 : 1 / 1.12)));
    state.tx = mx - ((mx - state.tx) * k) / state.k;
    state.ty = my - ((my - state.ty) * k) / state.k;
    state.k = k;
    applyView();
  };
  svg.onpointerdown = (evt) => {
    const target = nodes.find((n) => n.g.contains(evt.target));
    state.moved = false;
    if (target) { state.dragging = target; target.fixed = true; state.alpha = Math.max(state.alpha, 0.3); kick(); }
    else { state.panning = { x: evt.clientX - state.tx, y: evt.clientY - state.ty }; svg.classList.add("panning"); }
    svg.setPointerCapture(evt.pointerId);
  };
  svg.onpointermove = (evt) => {
    if (state.dragging) { const p = toGraph(evt); state.dragging.x = p.x; state.dragging.y = p.y; state.moved = true; }
    else if (state.panning) { state.tx = evt.clientX - state.panning.x; state.ty = evt.clientY - state.panning.y; state.moved = true; applyView(); }
  };
  svg.onpointerup = () => {
    if (state.dragging) {
      const n = state.dragging;
      n.fixed = false;
      if (!state.moved) setTriggerValue("selected", n.id);
    }
    state.dragging = null; state.panning = null; svg.classList.remove("panning");
  };

  // Force simulation: repulsion between all nodes, springs along edges, gentle pull to the centre
  let frame = null;
  function tick() {
    const a = state.alpha;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const p = nodes[i], q = nodes[j];
        let dx = q.x - p.x, dy = q.y - p.y, d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 160000) continue;
        const f = (900 * a) / d2, d = Math.sqrt(d2);
        dx /= d; dy /= d;
        p.vx -= dx * f * d * 0.05; p.vy -= dy * f * d * 0.05;
        q.vx += dx * f * d * 0.05; q.vy += dy * f * d * 0.05;
      }
    }
    edges.forEach((e) => {
      const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = ((d - 90) / d) * 0.06 * a;
      e.s.vx += dx * f; e.s.vy += dy * f; e.t.vx -= dx * f; e.t.vy -= dy * f;
    });
    nodes.forEach((n) => {
      n.vx += (width / 2 - n.x) * 0.004 * a; n.vy += (height / 2 - n.y) * 0.004 * a;
      if (!n.fixed) { n.x += n.vx; n.y += n.vy; }
      n.vx *= 0.6; n.vy *= 0.6;
    });
    draw();
    state.alpha *= 0.985;
    frame = state.alpha > 0.02 || state.dragging ? requestAnimationFrame(tick) : null;
  }
  function kick() { if (!frame) frame = requestAnimationFrame(tick); }
  function draw() {
    edges.forEach((e) => {
      e.line.setAttribute("x1", e.s.x); e.line.setAttribute("y1", e.s.y);
      e.line.setAttribute("x2", e.t.x); e.line.setAttribute("y2", e.t.y);
      e.label.setAttribute("x", (e.s.x + e.t.x) / 2); e.label.setAttribute("y", (e.s.y + e.t.y) / 2 - 3);
    });
    nodes.forEach((n) => n.g.setAttribute("transform", `translate(${n.x},${n.y})`));
  }
  function highlight(selectedId) {
    const near = selectedId && neighbours.get(selectedId);
    nodes.forEach((n) => {
      n.g.classList.toggle("selected", n.id === selectedId);
      n.g.classList.toggle("dim", Boolean(near) && !near.has(n.id));
    });
    edges.forEach((e) => {
      const hot = Boolean(selectedId) && (e.source === selectedId || e.target === selectedId);
      e.line.classList.toggle("hot", hot); e.label.classList.toggle("hot", hot);
    });
  }

  applyView();
  kick();
  return { highlight, stop: () => frame && cancelAnimationFrame(frame) };
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".kg-wrap");
  if (!root) return;
  const signature = JSON.stringify([data?.nodes?.map((n) => n.id), data?.edges?.length]);
  let inst = instances.get(parentElement);
  if (!inst || inst.signature !== signature) {
    if (inst) inst.graph.stop();
    inst = { signature, graph: build(root, data || {}, setTriggerValue) };
    instances.set(parentElement, inst);
  }
  inst.graph.highlight(data?.selected || null);
  return () => { inst.graph.stop(); instances.delete(parentElement); };
}
"""

_GRAPH_COMPONENT = st.components.v2.component("knowledge_graph_view", html=_HTML, css=_CSS, js=_JS)


def graph_view(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    selected: str | None = None,
    key: str = "kg_view",
    on_select: Callable[[], None] | None = None,
):
    """Mounts the graph. The clicked node id is in `st.session_state[key].selected` inside `on_select`."""
    return _GRAPH_COMPONENT(
        data={"nodes": nodes, "edges": edges, "selected": selected, "colors": ENTITY_COLORS},
        key=key,
        on_selected_change=on_select or (lambda: None),
    )
