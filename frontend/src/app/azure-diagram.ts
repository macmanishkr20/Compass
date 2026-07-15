// Azure architecture "compiler": takes a structured diagram spec (which the
// model can produce reliably) and computes a clean, non-overlapping layout,
// then renders it two ways from the SAME layout — an inline SVG preview and an
// editable draw.io / diagrams.net document. Layout is done by ELK's `layered`
// algorithm with compound (group) nodes — the same engine the Azure
// Architecture Diagram Builder uses — so ranks, spacing, and group boxes are
// computed properly and align. Icons are embedded as data: URIs so they always
// render, and never overlap the way raw model-authored XML does.

import ELK from 'elkjs/lib/elk.bundled.js';
import { costOf, iconDataUri, resolveIcon } from './azure-icons';

export interface SpecNode {
  id: string;
  service: string;
  label?: string;
  group?: string;
}
export interface SpecGroup {
  id: string;
  label?: string;
  parent?: string;
}
export interface SpecEdge {
  from: string;
  to: string;
  label?: string;
  dashed?: boolean;
}
export interface DiagramSpec {
  title?: string;
  groups?: SpecGroup[];
  nodes: SpecNode[];
  edges?: SpecEdge[];
}

// Laid-out primitives (absolute coordinates for SVG; draw.io uses the same
// absolute geometry with a flat parent so routing stays predictable).
interface Box {
  id: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
}
interface NodeBox extends Box {
  service: string;
}
interface Laid {
  title: string;
  width: number;
  height: number;
  groups: Box[];
  nodes: NodeBox[];
  edges: Array<{ from: string; to: string; label: string; dashed: boolean }>;
  byId: Map<string, NodeBox>;
}

// Layout constants (mirrors the reference builder's ELK tuning).
const NODE_W = 168;
const NODE_H = 112; // white service card (icon + label + cost badge)
const NODE_SPACING = 90; // between nodes within a rank
const RANK_SPACING = 150; // between ranks (room for sticky-note edge labels)
const GROUP_PAD = 30; // inner padding of a group box
const HEADER = 30; // extra top padding for the group title
const CANVAS_PAD = 52; // outer margin (room for cost badges / labels)

// Soft zone tints (fill + title), cycled per container — echoes the tinted
// lanes of a hand-built Azure diagram.
const ZONE_TINTS: Array<{ fill: string; stroke: string; title: string }> = [
  { fill: '#EEF3F8', stroke: '#9FB6CC', title: '#4A6076' },
  { fill: '#EAF2FB', stroke: '#8FB4E0', title: '#2C6FBF' },
  { fill: '#FDEDED', stroke: '#E7A9A9', title: '#C0392B' },
  { fill: '#EAF7EF', stroke: '#9FD9B4', title: '#2E8B57' },
  { fill: '#F3EEFA', stroke: '#C3AEE0', title: '#7A4EA8' },
  { fill: '#FBF3E6', stroke: '#E4C892', title: '#9C6F1E' },
];

/** Parse the model's spec block. Tolerant: returns null on invalid JSON. */
export function parseSpec(code: string): DiagramSpec | null {
  const t = code.trim();
  try {
    const obj = JSON.parse(t);
    if (obj && Array.isArray(obj.nodes)) return obj as DiagramSpec;
  } catch {
    /* fall through */
  }
  return null;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ElkNodeT = any;
const elk = new ELK();

/**
 * Lay the spec out with ELK's layered algorithm and compound group nodes, then
 * flatten ELK's parent-relative coordinates to absolute Box positions for our
 * renderers. Groups nest arbitrarily (subscription → VNet → subnet); a group
 * with no descendant services is dropped so ELK never sees an empty compound.
 */
export async function layout(spec: DiagramSpec): Promise<Laid> {
  const allGroups = spec.groups ?? [];
  const nodes = spec.nodes ?? [];
  const nodeById = new Map(nodes.map((n) => [n.id, n]));

  const gid = (v: string | undefined) => v ?? '';
  const childGroupsOf = (pid: string | undefined) =>
    allGroups.filter((g) => gid(g.parent) === gid(pid));
  const nodesOf = (g: string | undefined) =>
    nodes.filter((n) => gid(n.group) === gid(g));

  // A group counts only if it (recursively) holds at least one node.
  const hasContent = (id: string): boolean =>
    nodesOf(id).length > 0 || childGroupsOf(id).some((c) => hasContent(c.id));
  const groups = allGroups.filter((g) => hasContent(g.id));
  const groupById = new Map(groups.map((g) => [g.id, g]));
  const keepGroup = (id: string) => groupById.has(id);

  // Build ELK children for a container id (undefined = root).
  function buildChildren(container: string | undefined): ElkNodeT[] {
    const subGroups = childGroupsOf(container)
      .filter((g) => keepGroup(g.id))
      .map((g) => ({
        id: g.id,
        layoutOptions: {
          'elk.padding': `[top=${GROUP_PAD + HEADER},left=${GROUP_PAD},bottom=${GROUP_PAD},right=${GROUP_PAD}]`,
          'elk.spacing.nodeNode': String(NODE_SPACING),
          'elk.layered.spacing.nodeNodeBetweenLayers': String(RANK_SPACING),
        },
        children: buildChildren(g.id),
      }));
    const memberNodes = nodesOf(container).map((n) => ({
      id: n.id,
      width: NODE_W,
      height: NODE_H,
    }));
    return [...subGroups, ...memberNodes];
  }

  const edgeList = (spec.edges ?? []).filter(
    (e) => nodeById.has(e.from) && nodeById.has(e.to),
  );

  const root: ElkNodeT = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.spacing.nodeNode': String(NODE_SPACING),
      'elk.layered.spacing.nodeNodeBetweenLayers': String(RANK_SPACING),
      'elk.spacing.edgeNode': '40',
      'elk.spacing.edgeEdge': '24',
      'elk.hierarchyHandling': 'INCLUDE_CHILDREN',
      'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.padding': `[top=${CANVAS_PAD},left=${CANVAS_PAD},bottom=${CANVAS_PAD},right=${CANVAS_PAD}]`,
    },
    children: buildChildren(undefined),
    edges: edgeList.map((e, i) => ({
      id: 'e' + i,
      sources: [e.from],
      targets: [e.to],
    })),
  };

  const res: ElkNodeT = await elk.layout(root);

  // Flatten ELK's parent-relative coordinates to absolute boxes.
  const groupBoxes: Box[] = [];
  const nodeBoxes: NodeBox[] = [];
  const num = (v: unknown, d: number) =>
    typeof v === 'number' && isFinite(v) ? v : d;

  function walk(node: ElkNodeT, ox: number, oy: number): void {
    for (const child of node.children ?? []) {
      const ax = ox + num(child.x, 0);
      const ay = oy + num(child.y, 0);
      if (groupById.has(child.id)) {
        const g = groupById.get(child.id)!;
        groupBoxes.push({
          id: g.id,
          label: g.label ?? '',
          x: ax,
          y: ay,
          w: num(child.width, 240),
          h: num(child.height, 160),
        });
        walk(child, ax, ay);
      } else if (nodeById.has(child.id)) {
        const n = nodeById.get(child.id)!;
        nodeBoxes.push({
          id: n.id,
          service: n.service,
          label: n.label ?? resolveIcon(n.service).name,
          x: ax,
          y: ay,
          w: num(child.width, NODE_W),
          h: num(child.height, NODE_H),
        });
      }
    }
  }
  walk(res, 0, 0);

  const byId = new Map<string, NodeBox>();
  for (const n of nodeBoxes) byId.set(n.id, n);
  const edges = edgeList.map((e) => ({
    from: e.from,
    to: e.to,
    label: e.label ?? '',
    dashed: !!e.dashed,
  }));

  // Outer size ELK reports already includes CANVAS_PAD; add a hair for the
  // cost badges that overhang node tops.
  return {
    title: spec.title ?? 'Azure Architecture',
    width: num(res.width, 800) + 12,
    height: num(res.height, 600) + 12,
    groups: groupBoxes,
    nodes: nodeBoxes,
    edges,
    byId,
  };
}

// ---- edge routing (shared shape logic) -----------------------------------

/** Orthogonal L-route between two node boxes: exit the source on the side
 * facing the target, enter the target on its facing side, one elbow. */
function route(a: Box, b: Box): { pts: Array<[number, number]>; mid: [number, number] } {
  const ac = [a.x + a.w / 2, a.y + a.h / 2];
  const bc = [b.x + b.w / 2, b.y + b.h / 2];
  const dx = bc[0] - ac[0];
  const dy = bc[1] - ac[1];
  let start: [number, number];
  let end: [number, number];
  let elbow: [number, number];
  if (Math.abs(dx) >= Math.abs(dy)) {
    // horizontal-dominant: exit left/right, elbow at target x
    start = [dx >= 0 ? a.x + a.w : a.x, ac[1]];
    end = [dx >= 0 ? b.x : b.x + b.w, bc[1]];
    elbow = [end[0], start[1]];
  } else {
    // vertical-dominant: exit top/bottom, elbow at target y
    start = [ac[0], dy >= 0 ? a.y + a.h : a.y];
    end = [bc[0], dy >= 0 ? b.y : b.y + b.h];
    elbow = [start[0], end[1]];
  }
  const pts: Array<[number, number]> = [start, elbow, end];
  const mid: [number, number] = elbow;
  return { pts, mid };
}

function esc(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function wrap(label: string, max = 20): string[] {
  const words = label.split(/\s+/);
  const lines: string[] = [];
  let cur = '';
  for (const w of words) {
    if ((cur + ' ' + w).trim().length > max && cur) {
      lines.push(cur);
      cur = w;
    } else {
      cur = (cur + ' ' + w).trim();
    }
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 3);
}

// ---- inline SVG preview ----------------------------------------------------

export function toSvg(laid: Laid, dark: boolean): string {
  const ink = dark ? '#e8ecf1' : '#1f2733';
  const cardInk = '#26313d'; // cards stay light for icon legibility
  const line = dark ? '#6b7c8f' : '#7f93b0';
  const parts: string[] = [];
  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${laid.width} ${laid.height}" preserveAspectRatio="xMidYMid meet" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif">`,
  );
  parts.push(
    `<defs><marker id="arr" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto"><path d="M1 1L9 5 1 9z" fill="${line}"/></marker>` +
      `<filter id="cardsh" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="1.5" stdDeviation="2.5" flood-color="#0b1a2b" flood-opacity="0.16"/></filter></defs>`,
  );

  // tinted zone containers (outer first so nesting reads correctly)
  const sorted = [...laid.groups].sort((a, b) => b.w * b.h - a.w * a.h);
  sorted.forEach((g, i) => {
    const t = ZONE_TINTS[i % ZONE_TINTS.length];
    parts.push(
      `<rect x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" rx="14" fill="${t.fill}" fill-opacity="${dark ? 0.14 : 0.75}" stroke="${t.stroke}" stroke-width="1.3" stroke-dasharray="6 5"/>`,
    );
    if (g.label)
      parts.push(
        `<text x="${g.x + 16}" y="${g.y + 22}" fill="${t.title}" font-size="13" font-weight="700" letter-spacing="0.02em">${esc(g.label)}</text>`,
      );
  });

  // edges with sticky-note labels
  for (const e of laid.edges) {
    const a = laid.byId.get(e.from)!;
    const b = laid.byId.get(e.to)!;
    const { pts, mid } = route(a, b);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ');
    parts.push(
      `<path d="${d}" fill="none" stroke="${line}" stroke-width="1.6" stroke-dasharray="5 4" marker-end="url(#arr)"/>`,
    );
    if (e.label) {
      const lines = wrap(e.label, 22);
      const w = Math.max(...lines.map((l) => l.length)) * 5.9 + 16;
      const h = lines.length * 13 + 10;
      const lx = mid[0] - w / 2;
      const ly = mid[1] - h / 2;
      parts.push(
        `<rect x="${lx}" y="${ly}" width="${w}" height="${h}" rx="5" fill="#FFF6D6" stroke="#E7C766" stroke-width="1"/>`,
      );
      lines.forEach((ln, i) => {
        parts.push(
          `<text x="${mid[0]}" y="${ly + 15 + i * 13}" fill="#7a5b12" font-size="10.5" text-anchor="middle">${esc(ln)}</text>`,
        );
      });
    }
  }

  // service cards (white card + official icon + label + cost badge)
  for (const n of laid.nodes) {
    const cx = n.x + n.w / 2;
    parts.push(
      `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="12" fill="#ffffff" stroke="#dfe6ee" stroke-width="1" filter="url(#cardsh)"/>`,
    );
    // icon
    parts.push(
      `<image href="${iconDataUri(n.service)}" x="${cx - 21}" y="${n.y + 12}" width="42" height="42" preserveAspectRatio="xMidYMid meet"/>`,
    );
    // label
    const lines = wrap(n.label, 20);
    lines.forEach((ln, i) => {
      parts.push(
        `<text x="${cx}" y="${n.y + 70 + i * 13}" fill="${cardInk}" font-size="11.5" font-weight="600" text-anchor="middle">${esc(ln)}</text>`,
      );
    });
    // cost badge
    const cost = costOf(n.service);
    if (cost != null && cost > 0) {
      const label = '$' + (Number.isInteger(cost) ? cost : cost.toFixed(2)) + '/mo';
      const bw = label.length * 6.1 + 14;
      const bx = n.x + n.w - bw + 6;
      const by = n.y - 9;
      parts.push(
        `<rect x="${bx}" y="${by}" width="${bw}" height="18" rx="9" fill="#2ea44f"/>` +
          `<text x="${bx + bw / 2}" y="${by + 12.5}" fill="#ffffff" font-size="10" font-weight="700" text-anchor="middle">${esc(label)}</text>`,
      );
    }
  }

  parts.push('</svg>');
  return parts.join('');
}

// ---- draw.io / diagrams.net export ----------------------------------------

export function toDrawio(laid: Laid): string {
  const cells: string[] = [
    '<mxCell id="0"/>',
    '<mxCell id="1" parent="0"/>',
  ];
  // containers (absolute geometry, parent=1 — flat so edge routing is stable),
  // tinted like the inline zones
  const sortedG = [...laid.groups].sort((a, b) => b.w * b.h - a.w * a.h);
  sortedG.forEach((g, i) => {
    const t = ZONE_TINTS[i % ZONE_TINTS.length];
    const style =
      `rounded=1;whiteSpace=wrap;html=1;dashed=1;dashPattern=6 5;fillColor=${t.fill};` +
      `opacity=60;strokeColor=${t.stroke};verticalAlign=top;fontStyle=1;fontColor=${t.title};fontSize=13;`;
    cells.push(
      `<mxCell id="${esc(g.id)}" value="${esc(g.label)}" style="${style}" vertex="1" parent="1">` +
        `<mxGeometry x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" as="geometry"/></mxCell>`,
    );
  });
  // nodes as image shapes with embedded data-URI icons + label below; label
  // carries the cost estimate so the draw.io export mirrors the preview
  for (const n of laid.nodes) {
    const uri = iconDataUri(n.service);
    const cost = costOf(n.service);
    const value =
      cost != null && cost > 0
        ? `${n.label} ($${Number.isInteger(cost) ? cost : cost.toFixed(2)}/mo)`
        : n.label;
    const style =
      `shape=image;html=1;verticalLabelPosition=bottom;verticalAlign=top;` +
      `labelBackgroundColor=none;imageAspect=1;aspect=fixed;image=${uri};fontSize=12;`;
    const ix = n.x + n.w / 2 - 24;
    cells.push(
      `<mxCell id="${esc(n.id)}" value="${esc(value)}" style="${style}" vertex="1" parent="1">` +
        `<mxGeometry x="${ix}" y="${n.y + 8}" width="48" height="48" as="geometry"/></mxCell>`,
    );
  }
  // edges
  let ei = 0;
  for (const e of laid.edges) {
    const style =
      'edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;' +
      (e.dashed ? 'dashed=1;' : '') +
      'fontSize=11;';
    cells.push(
      `<mxCell id="e${ei++}" value="${esc(e.label)}" style="${style}" edge="1" parent="1" source="${esc(e.from)}" target="${esc(e.to)}">` +
        `<mxGeometry relative="1" as="geometry"/></mxCell>`,
    );
  }
  const model =
    `<mxGraphModel dx="${laid.width}" dy="${laid.height}" grid="1" gridSize="10" guides="1" ` +
    `arrows="1" fold="1" page="1" pageWidth="${laid.width + 80}" pageHeight="${laid.height + 80}">` +
    `<root>${cells.join('')}</root></mxGraphModel>`;
  return (
    `<mxfile host="app.diagrams.net" type="device">` +
    `<diagram name="${esc(laid.title)}" id="compass-azure">${model}</diagram></mxfile>`
  );
}
