// Azure architecture "compiler": takes a structured diagram spec (which the
// model can produce reliably) and computes a clean, non-overlapping layout,
// then renders it two ways from the SAME layout — an inline SVG preview and an
// editable draw.io / diagrams.net document. Because WE compute coordinates and
// embed the icons as data: URIs, the result can never overlap the way raw
// model-authored XML does, and the Azure icons always render.

import { iconDataUri, iconSvg, resolveIcon } from './azure-icons';

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
const NODE_W = 148;
const NODE_H = 96;
const COL_GAP = 34;
const ROW_GAP = 40;
const COLS = 3; // nodes per row inside a group
const PAD = 22; // inner padding of a group
const HEADER = 30; // group title band
const GROUP_GAP = 34; // between sibling groups
const CANVAS_PAD = 40;

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
  const sub = dark ? '#9fb0c3' : '#5a6b7b';
  const line = dark ? '#5b6b7d' : '#8a99a8';
  const groupStroke = '#3b8ed8';
  const parts: string[] = [];
  parts.push(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${laid.width} ${laid.height}" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif">`,
  );
  parts.push(
    `<defs><marker id="arr" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto"><path d="M1 1L7.5 4.5 1 8z" fill="${line}"/></marker></defs>`,
  );

  // group containers (outer first so nesting reads correctly)
  const sorted = [...laid.groups].sort((a, b) => b.w * b.h - a.w * a.h);
  for (const g of sorted) {
    parts.push(
      `<rect x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" rx="12" fill="none" stroke="${groupStroke}" stroke-width="1.4" stroke-dasharray="5 5" opacity="0.9"/>`,
    );
    if (g.label)
      parts.push(
        `<text x="${g.x + 14}" y="${g.y + 20}" fill="${groupStroke}" font-size="12.5" font-weight="700">${esc(g.label)}</text>`,
      );
  }

  // edges
  for (const e of laid.edges) {
    const a = laid.byId.get(e.from)!;
    const b = laid.byId.get(e.to)!;
    const { pts, mid } = route(a, b);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ');
    parts.push(
      `<path d="${d}" fill="none" stroke="${line}" stroke-width="1.5"${e.dashed ? ' stroke-dasharray="4 4"' : ''} marker-end="url(#arr)"/>`,
    );
    if (e.label) {
      const w = e.label.length * 6.2 + 10;
      parts.push(
        `<rect x="${mid[0] - w / 2}" y="${mid[1] - 9}" width="${w}" height="16" rx="4" fill="${dark ? '#141a22' : '#ffffff'}" opacity="0.92"/>` +
          `<text x="${mid[0]}" y="${mid[1] + 2.5}" fill="${sub}" font-size="10.5" text-anchor="middle">${esc(e.label)}</text>`,
      );
    }
  }

  // nodes (icon + wrapped label)
  for (const n of laid.nodes) {
    const cx = n.x + n.w / 2;
    const iconX = cx - 24;
    const iconY = n.y;
    // inline the tile SVG scaled to 48
    const tile = iconSvg(n.service)
      .replace(/^<svg[^>]*>/, '')
      .replace(/<\/svg>$/, '');
    parts.push(
      `<g transform="translate(${iconX},${iconY})">${tile}</g>`,
    );
    const lines = wrap(n.label);
    lines.forEach((ln, i) => {
      parts.push(
        `<text x="${cx}" y="${n.y + 62 + i * 13}" fill="${ink}" font-size="11.5" font-weight="500" text-anchor="middle">${esc(ln)}</text>`,
      );
    });
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
  // containers (absolute geometry, parent=1 — flat so edge routing is stable)
  for (const g of laid.groups) {
    const style =
      'rounded=1;whiteSpace=wrap;html=1;dashed=1;strokeColor=#3B8ED8;fillColor=none;' +
      'verticalAlign=top;fontStyle=1;fontColor=#3B8ED8;fontSize=13;dashPattern=6 6;';
    cells.push(
      `<mxCell id="${esc(g.id)}" value="${esc(g.label)}" style="${style}" vertex="1" parent="1">` +
        `<mxGeometry x="${g.x}" y="${g.y}" width="${g.w}" height="${g.h}" as="geometry"/></mxCell>`,
    );
  }
  // nodes as image shapes with embedded data-URI icons + label below
  for (const n of laid.nodes) {
    const uri = iconDataUri(n.service);
    const style =
      `sketch=0;html=1;image;fixedSize=1;shape=image;verticalLabelPosition=bottom;` +
      `verticalAlign=top;imageAspect=0;aspect=fixed;image=${uri};fontSize=12;`;
    const ix = n.x + n.w / 2 - 24;
    cells.push(
      `<mxCell id="${esc(n.id)}" value="${esc(n.label)}" style="${style}" vertex="1" parent="1">` +
        `<mxGeometry x="${ix}" y="${n.y}" width="48" height="48" as="geometry"/></mxCell>`,
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
