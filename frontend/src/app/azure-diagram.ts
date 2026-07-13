// Azure architecture "compiler": takes a structured diagram spec (which the
// model can produce reliably) and computes a clean, non-overlapping layout,
// then renders it two ways from the SAME layout — an inline SVG preview and an
// editable draw.io / diagrams.net document. Because WE compute coordinates and
// embed the icons as data: URIs, the result can never overlap the way raw
// model-authored XML does, and the Azure icons always render.

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

// Layout constants.
const NODE_W = 158;
const NODE_H = 108; // white service card (icon + label + cost badge)
const COL_GAP = 66; // room for sticky-note edge labels between columns
const ROW_GAP = 60;
const COLS = 3; // nodes per row inside a group
const PAD = 26; // inner padding of a group
const HEADER = 34; // group title band
const GROUP_GAP = 40; // between sibling groups
const CANVAS_PAD = 44;

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

/**
 * Recursively size and place groups and nodes. Each group lays out its child
 * groups first (as a wrapping row band), then its direct nodes (a grid). Sizes
 * bubble up so every container encloses its children with padding — nothing can
 * overlap because siblings are packed with fixed gaps.
 */
export function layout(spec: DiagramSpec): Laid {
  const groups = spec.groups ?? [];
  const childGroupsOf = (pid: string | undefined) =>
    groups.filter((g) => (g.parent ?? '') === (pid ?? ''));
  const nodesOf = (gid: string | undefined) =>
    spec.nodes.filter((n) => (n.group ?? '') === (gid ?? ''));

  const groupBoxes: Box[] = [];
  const nodeBoxes: NodeBox[] = [];

  // Measure + place a container's children starting at local origin; returns
  // the content size. Placement writes absolute coords via the (ox, oy) offset.
  function place(
    gid: string | undefined,
    ox: number,
    oy: number,
  ): { w: number; h: number } {
    const subGroups = childGroupsOf(gid);
    const dirNodes = nodesOf(gid);
    let cursorY = oy;
    let contentW = 0;

    // 1) Direct nodes first (top): a COLS-wide grid, so entry-point services
    //    read above the nested groups they hand off to.
    if (dirNodes.length) {
      const cols = Math.min(COLS, dirNodes.length);
      dirNodes.forEach((n, i) => {
        const c = i % cols;
        const r = Math.floor(i / cols);
        const nx = ox + c * (NODE_W + COL_GAP);
        const ny = cursorY + r * (NODE_H + ROW_GAP);
        nodeBoxes.push({
          id: n.id,
          service: n.service,
          label: n.label ?? resolveIcon(n.service).name,
          x: nx,
          y: ny,
          w: NODE_W,
          h: NODE_H,
        });
      });
      const rows = Math.ceil(dirNodes.length / cols);
      contentW = Math.max(contentW, cols * NODE_W + (cols - 1) * COL_GAP);
      cursorY += rows * NODE_H + (rows - 1) * ROW_GAP;
    }

    // 2) Child-group band below: lay siblings left→right, wrapping when wide.
    if (subGroups.length) {
      if (dirNodes.length) cursorY += GROUP_GAP;
      const MAX_ROW = 3; // groups per row
      let rowX = ox;
      let rowStartY = cursorY;
      let rowH = 0;
      let inRow = 0;
      for (const sg of subGroups) {
        const size = measure(sg.id);
        if (inRow >= MAX_ROW) {
          cursorY = rowStartY + rowH + GROUP_GAP;
          rowX = ox;
          rowStartY = cursorY;
          rowH = 0;
          inRow = 0;
        }
        placeAt(sg, rowX, rowStartY, size);
        rowX += size.w + GROUP_GAP;
        rowH = Math.max(rowH, size.h);
        contentW = Math.max(contentW, rowX - GROUP_GAP - ox);
        inRow++;
      }
      cursorY = rowStartY + rowH;
    }

    return { w: contentW, h: cursorY - oy };
  }

  // measure returns the full outer size of a group (header + padded content).
  function measure(gid: string): { w: number; h: number } {
    // Dry-run place into throwaway arrays to get content size.
    const savedG = groupBoxes.length;
    const savedN = nodeBoxes.length;
    const content = place(gid, 0, 0);
    // discard the dry-run placements
    groupBoxes.length = savedG;
    nodeBoxes.length = savedN;
    return {
      w: Math.max(content.w, 140) + PAD * 2,
      h: content.h + HEADER + PAD * 2,
    };
  }

  // placeAt commits a group box at (x,y) and lays its children inside.
  function placeAt(
    g: SpecGroup,
    x: number,
    y: number,
    size: { w: number; h: number },
  ): void {
    groupBoxes.push({
      id: g.id,
      label: g.label ?? '',
      x,
      y,
      w: size.w,
      h: size.h,
    });
    place(g.id, x + PAD, y + HEADER + PAD);
  }

  // Top level: root groups (band) then ungrouped nodes.
  const content = place(undefined, CANVAS_PAD, CANVAS_PAD);
  const width = content.w + CANVAS_PAD * 2;
  const height = content.h + CANVAS_PAD * 2;

  const byId = new Map<string, NodeBox>();
  for (const n of nodeBoxes) byId.set(n.id, n);
  const edges = (spec.edges ?? [])
    .filter((e) => byId.has(e.from) && byId.has(e.to))
    .map((e) => ({
      from: e.from,
      to: e.to,
      label: e.label ?? '',
      dashed: !!e.dashed,
    }));

  return {
    title: spec.title ?? 'Azure Architecture',
    width,
    height,
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
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${laid.width} ${laid.height}" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif">`,
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
