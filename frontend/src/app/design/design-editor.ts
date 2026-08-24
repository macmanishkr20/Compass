/**
 * The canvas editor agent.
 *
 * A design is model-authored HTML, so it renders in a sandboxed iframe with no
 * same-origin access — the parent document can't reach into it. Direct
 * manipulation therefore happens *inside* the frame: this script is injected
 * into the preview copy of the document (never into what is stored or
 * exported) and talks to the panel over postMessage.
 *
 * It owns hover outlines, selection, drag, resize, nudge, delete, inline text
 * editing, alignment, and comment pins. Everything it adds is marked
 * `data-dz`, so serialising back out is a matter of dropping those nodes.
 */

/** Parent → frame. */
export interface EditorCommand {
  dz:
    | 'mode' | 'align' | 'delete' | 'pins' | 'flush' | 'deselect' | 'pointer'
    | 'tweak' | 'ping' | 'grab' | 'replace'
    | 'style' | 'text' | 'attr' | 'css' | 'tree' | 'pick' | 'insert';
  /** Declarations to set on the selection; '' removes one. */
  decls?: Record<string, string>;
  /** Replacement text, for a leaf element. */
  text?: string;
  /** An attribute to set, and what to set it to. */
  name?: string;
  value?: string;
  /** A node in the layer tree, by the id the tree reported. */
  tid?: number;
  /** Markup to put in place of the selection, for an asked-for change. */
  html?: string;
  /** The custom property a tweak sets, and what to set it to. */
  tweakVar?: string;
  tweakValue?: string;
  mode?: 'view' | 'inspect' | 'comment' | 'edit';
  align?: 'left' | 'center' | 'right' | 'top' | 'middle' | 'bottom';
  pins?: Array<{ id: string; x: number; y: number; text: string }>;
  /** Forwarded pointer input, in the frame's own client coordinates. Used when
   *  the embedding delivers the event to the iframe element rather than into
   *  the frame — see the note on the parent's forwarding. */
  kind?: 'down' | 'move' | 'up' | 'click' | 'dblclick' | 'text' | 'box';
  x?: number;
  y?: number;
}

/** What the inspector reports about the selected element. */
export interface EditorDetails {
  tag: string;
  id: string;
  classes: string;
  size: string;
  font: string;
  color: string;
  background: string;
  spacing: string;
  text: string;
}

/** Every property the inspector panel shows, resolved from the cascade. */
export interface EditorProps {
  tag: string; id: string; classes: string;
  leaf: boolean; text: string;
  font: string; fontSize: number; color: string; weight: string;
  italic: boolean; underline: boolean; strike: boolean;
  align: string; leading: number; tracking: string; transform: string;
  width: number; height: number;
  widthMode: 'hug' | 'fixed' | 'fill';
  heightMode: 'hug' | 'fixed' | 'fill';
  grow: number; alignSelf: string; position: string; zIndex: string;
  padding: number[]; margin: number[];
  display: string; direction: string; gap: string; justify: string; items: string;
  background: string; radius: number; overflow: string; opacity: number;
  textShadow: string; boxShadow: string;
  borderWidth: number; borderStyle: string; borderColor: string;
  css: string;
  inline: string; attrs: Record<string, string>;
  inFlex: boolean; svg: boolean;
}

/** One row of the layer tree. */
export interface EditorNode {
  tid: number;
  depth: number;
  name: string;
  tag: string;
  kids: number;
  swatch: string;
  on: boolean;
}

/** Where the selection sits, in the frame's own coordinates. */
export interface EditorRect {
  x: number;
  y: number;
  w: number;
  h: number;
  svg: boolean;   // an SVG node: it moves, but it has no box to resize
}

/** Frame → parent. */
/** A knob the design declares as tweakable. */
export interface EditorTweak {
  name: string;
  type: string;
  var: string;
  value: string;
  options?: string[];
}

export interface EditorEvent {
  dz:
    | 'selected' | 'html' | 'comment' | 'ready' | 'typing' | 'typed'
    | 'tweaks' | 'pong' | 'pageerror' | 'grabbed' | 'tree';
  /** Everything the inspector shows about the selection. */
  props?: EditorProps;
  /** The document's shape, for the layer tree. */
  nodes?: EditorNode[];
  message?: string;                   // what the design's own script threw
  path?: string;                      // where the grabbed element sits, for a photo
  tweaks?: EditorTweak[];
  label?: string;                     // e.g. "section.hero"
  rect?: EditorRect;                  // where to put the floating toolbar
  details?: EditorDetails;            // populated in inspect and edit modes
  html?: string;                      // the document, editor artifacts removed
  x?: number;                         // comment position, as a 0..1 fraction
  y?: number;
}

export const EDITOR_SCRIPT = String.raw`
(function () {
  // 'view' is the default: the design behaves like a page. Nothing here
  // touches the document until a tool is picked.
  var mode = 'view';
  var sel = null;      // the selected element
  var box = null;      // the overlay drawn around it
  var pinLayer = null;
  var drag = null;     // { kind, startX, startY, rect, left, top, w, h }

  function mark(el) { el.setAttribute('data-dz', '1'); return el; }

  function css(text) {
    var s = mark(document.createElement('style'));
    s.textContent = text;
    document.head.appendChild(s);
  }

  css(
    '[data-dz-hover]{outline:2px solid rgba(0,113,227,.55)!important;outline-offset:1px}' +
    '#dz-box{position:absolute;z-index:2147483000;border:2px solid #0071E3;pointer-events:none}' +
    '#dz-box .dz-h{position:absolute;width:9px;height:9px;background:#fff;border:1.5px solid #0071E3;border-radius:2px;pointer-events:auto}' +
    '#dz-box .dz-h.nw{left:-5px;top:-5px;cursor:nwse-resize}' +
    '#dz-box .dz-h.n{left:calc(50% - 5px);top:-5px;cursor:ns-resize}' +
    '#dz-box .dz-h.ne{right:-5px;top:-5px;cursor:nesw-resize}' +
    '#dz-box .dz-h.e{right:-5px;top:calc(50% - 5px);cursor:ew-resize}' +
    '#dz-box .dz-h.se{right:-5px;bottom:-5px;cursor:nwse-resize}' +
    '#dz-box .dz-h.s{left:calc(50% - 5px);bottom:-5px;cursor:ns-resize}' +
    '#dz-box .dz-h.sw{left:-5px;bottom:-5px;cursor:nesw-resize}' +
    '#dz-box .dz-h.w{left:-5px;top:calc(50% - 5px);cursor:ew-resize}' +
    '#dz-box .dz-move{position:absolute;inset:0;cursor:move;pointer-events:auto}' +
    '#dz-box.inspect{border-color:#0071E3;border-style:dashed}' +
    '#dz-box.inspect .dz-h{display:none}' +
    '#dz-box.inspect .dz-move{cursor:default}' +
    '#dz-pins{position:absolute;left:0;top:0;width:100%;height:100%;z-index:2147482000;pointer-events:none}' +
    '.dz-pin{position:absolute;transform:translate(-50%,-100%);background:#0071E3;color:#fff;' +
      'font:600 11px/1 -apple-system,system-ui,sans-serif;padding:5px 7px;border-radius:9px 9px 9px 2px;' +
      'pointer-events:auto;cursor:default;max-width:220px;white-space:pre-wrap}' +
    'body.dz-comment,body.dz-comment *{cursor:crosshair!important}'
  );

  function post(msg) { parent.postMessage(msg, '*'); }

  // SVG has no CSS box: left/top and width/height do nothing there, so a node
  // inside a chart or an icon has to be moved with a transform instead.
  function isSvg(el) {
    return !!el && typeof SVGElement !== 'undefined' && el instanceof SVGElement &&
      el.tagName.toLowerCase() !== 'svg';
  }

  function editable(el) {
    return el && el.nodeType === 1 && el !== document.body &&
      el !== document.documentElement && !el.closest('[data-dz]');
  }

  // The selection overlay sits on top of what it frames, so a second click —
  // or a double-click to retype — lands on the overlay rather than the
  // element. Look past anything this editor drew.
  function beneath(x, y) {
    var list = document.elementsFromPoint ? document.elementsFromPoint(x, y) : [];
    for (var i = 0; i < list.length; i++) {
      if (!list[i].closest('[data-dz]')) return list[i];
    }
    return null;
  }

  function target(el, x, y) {
    return el && el.closest && el.closest('[data-dz]') ? beneath(x, y) : el;
  }

  // Where this element sits in the document, precisely enough to find it again
  // in a clean render of the same markup — used to photograph the selection.
  function pathTo(el) {
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      var name = el.tagName.toLowerCase();
      var parent = el.parentNode;
      if (parent && parent.children) {
        var same = [], i;
        for (i = 0; i < parent.children.length; i++) {
          if (parent.children[i].tagName === el.tagName) same.push(parent.children[i]);
        }
        if (same.length > 1) name += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      }
      parts.unshift(name);
      el = parent;
    }
    return parts.join(' > ');
  }

  function label(el) {
    var out = el.tagName.toLowerCase();
    if (el.id) return out + '#' + el.id;
    var cls = (el.getAttribute('class') || '').trim().split(/\s+/)[0];
    return cls ? out + '.' + cls : out;
  }

  // -- selection overlay ----------------------------------------------------
  function ensureBox() {
    if (box) return box;
    box = mark(document.createElement('div'));
    box.id = 'dz-box';
    var move = mark(document.createElement('div'));
    move.className = 'dz-move';
    box.appendChild(move);
    ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach(function (k) {
      var h = mark(document.createElement('div'));
      h.className = 'dz-h ' + k;
      h.dataset.dzHandle = k;
      box.appendChild(h);
    });
    document.body.appendChild(box);
    box.addEventListener('pointerdown', onGrab);
    return box;
  }

  function place() {
    if (!sel || !box) return;
    var r = sel.getBoundingClientRect();
    box.style.left = (r.left + scrollX) + 'px';
    box.style.top = (r.top + scrollY) + 'px';
    box.style.width = r.width + 'px';
    box.style.height = r.height + 'px';
  }

  function details(el) {
    var s = getComputedStyle(el);
    var r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      classes: (el.getAttribute('class') || '').trim(),
      size: Math.round(r.width) + ' × ' + Math.round(r.height),
      font: s.fontFamily.split(',')[0].replace(/["']/g, '') + ' ' +
            s.fontSize + ' / ' + s.fontWeight,
      color: s.color,
      background: s.backgroundColor,
      spacing: 'padding ' + s.padding + ' · margin ' + s.margin +
               ' · radius ' + s.borderRadius,
      text: (el.textContent || '').trim().slice(0, 120)
    };
  }


  // -- the property inspector ----------------------------------------------
  /** A colour as #RRGGBB, or '' when there is nothing painted. */
  function hex(c) {
    var m = (c || '').match(/[\d.]+/g);
    if (!m || m.length < 3) return '';
    if (m.length > 3 && Number(m[3]) === 0) return '';
    return '#' + m.slice(0, 3).map(function (v) {
      return ('0' + Math.round(Number(v)).toString(16)).slice(-2);
    }).join('').toUpperCase();
  }

  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : Math.round(n * 100) / 100;
  }

  /** hug / fixed / fill — how the box gets its size on one axis. */
  function sizing(el, cs, horizontal) {
    if (el.style[horizontal ? 'width' : 'height']) return 'fixed';
    var p = el.parentElement ? getComputedStyle(el.parentElement) : null;
    var flex = p && p.display.indexOf('flex') > -1;
    var column = flex && (p.flexDirection || 'row').indexOf('column') === 0;
    var alongMain = flex && (horizontal ? !column : column);
    if (alongMain && cs.flexGrow !== '0') return 'fill';
    if (flex && !alongMain && (cs.alignSelf === 'stretch' ||
        (cs.alignSelf === 'auto' && p.alignItems === 'stretch'))) return 'fill';
    if (horizontal && cs.display === 'block') return 'fill';
    return 'hug';
  }

  /** Line height as a multiple, the way a designer sets it. */
  function leading(cs) {
    if (cs.lineHeight === 'normal') return 0;
    return Math.round((parseFloat(cs.lineHeight) / parseFloat(cs.fontSize)) * 100) / 100;
  }


  /** The declarations that actually reach this element, in cascade order.
   *  A class-styled element has an empty style attribute, so showing only the
   *  inline ones would show an empty panel for a fully styled thing. */
  function matched(el) {
    var out = [], seen = {};
    for (var i = 0; i < document.styleSheets.length; i++) {
      var rules;
      try { rules = document.styleSheets[i].cssRules; } catch (e) { continue; }
      if (!rules) continue;
      for (var j = 0; j < rules.length; j++) {
        var rule = rules[j];
        if (!rule.selectorText || !rule.style) continue;
        var parts = rule.selectorText.split(',');
        for (var k = 0; k < parts.length; k++) {
          var one = parts[k].trim();
          if (!one || one.indexOf(':') > -1) continue;      // no states or pseudos
          var hit = false;
          try { hit = el.matches(one); } catch (e) { hit = false; }
          if (!hit) continue;
          for (var d = 0; d < rule.style.length; d++) {
            var name = rule.style[d];
            seen[name] = rule.style.getPropertyValue(name);
          }
          break;
        }
      }
    }
    Object.keys(seen).forEach(function (k) { out.push(k + ': ' + seen[k] + ';'); });
    return out.slice(0, 60).join('\n');
  }

  function props(el) {
    var cs = getComputedStyle(el);
    var r = el.getBoundingClientRect();
    var p = el.parentElement ? getComputedStyle(el.parentElement) : null;
    var leaf = el.children.length === 0;
    var attrs = {};
    for (var i = 0; i < el.attributes.length; i++) {
      var a = el.attributes[i];
      if (a.name === 'style' || a.name.indexOf('data-dz') === 0) continue;
      attrs[a.name] = a.value;
    }
    var side = function (which) {
      return ['Top', 'Right', 'Bottom', 'Left'].map(function (s) {
        return num(cs[which + s]);
      });
    };
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      classes: (el.getAttribute('class') || '').trim(),
      // text
      leaf: leaf,
      text: leaf ? (el.textContent || '') : '',
      font: cs.fontFamily,
      fontSize: num(cs.fontSize),
      color: hex(cs.color),
      weight: cs.fontWeight,
      italic: cs.fontStyle === 'italic',
      underline: (cs.textDecorationLine || '').indexOf('underline') > -1,
      strike: (cs.textDecorationLine || '').indexOf('line-through') > -1,
      align: cs.textAlign,
      leading: leading(cs),
      tracking: cs.letterSpacing === 'normal' ? '' : cs.letterSpacing,
      transform: cs.textTransform,
      // box
      width: Math.round(r.width),
      height: Math.round(r.height),
      widthMode: sizing(el, cs, true),
      heightMode: sizing(el, cs, false),
      grow: num(cs.flexGrow),
      alignSelf: cs.alignSelf,
      position: cs.position,
      zIndex: cs.zIndex,
      padding: side('padding'),
      margin: side('margin'),
      // how it lays its own children out
      display: cs.display,
      direction: cs.flexDirection,
      gap: cs.gap === 'normal' ? '0px' : cs.gap,
      justify: cs.justifyContent,
      items: cs.alignItems,
      // appearance
      background: hex(cs.backgroundColor),
      radius: num(cs.borderTopLeftRadius),
      overflow: cs.overflow,
      opacity: num(cs.opacity),
      textShadow: cs.textShadow === 'none' ? '' : cs.textShadow,
      boxShadow: cs.boxShadow === 'none' ? '' : cs.boxShadow,
      borderWidth: num(cs.borderTopWidth),
      borderStyle: cs.borderTopStyle,
      borderColor: hex(cs.borderTopColor),
      // What this element itself declares — minus the position this editor
      // hung a drag on, which is ours and not the design's.
      // what the cascade puts on it, for the Code tab
      css: matched(el),
      inline: (function () {
        var css = el.getAttribute('style') || '';
        if (!el.getAttribute('data-dz-pos')) return css;
        return css.replace(/(^|;)\s*position\s*:[^;]*;?/, '$1').trim();
      })(),
      attrs: attrs,
      inFlex: !!p && p.display.indexOf('flex') > -1,
      svg: isSvg(el)
    };
  }

  // -- the layer tree -------------------------------------------------------
  var tids = [];       // index -> element, rebuilt whenever the tree is asked for

  function nameOf(el) {
    if (el.children.length === 0) {
      var t = (el.textContent || '').trim();
      return t ? 'Text “' + t.slice(0, 18) + '”' : el.tagName.toLowerCase();
    }
    var cs = getComputedStyle(el);
    if (cs.display.indexOf('flex') > -1) {
      return (cs.flexDirection || 'row').indexOf('column') === 0 ? 'Column' : 'Row';
    }
    if (cs.display.indexOf('grid') > -1) return 'Grid';
    return 'group';
  }


  /** Drop a new element into the selection (or the body) and select it. */
  function insert(kind) {
    if (mode !== 'edit') return;
    var host = sel || document.body;
    if (host.closest && host.closest('[data-dz]')) host = document.body;
    var el = document.createElement('div');
    if (kind === 'text') {
      el.textContent = 'Text';
      el.style.cssText = 'font-size:16px;line-height:1.4;';
    } else {
      el.style.cssText = 'width:160px;height:96px;background:#E9E5E1;border-radius:8px;';
    }
    host.appendChild(el);
    select(el);
    flush();
  }

  function tree() {
    tids = [];
    var out = [];
    (function walk(el, depth) {
      for (var i = 0; i < el.children.length; i++) {
        var c = el.children[i];
        if (c.closest('[data-dz]') || c.tagName === 'SCRIPT' || c.tagName === 'STYLE') continue;
        var cs = getComputedStyle(c);
        tids.push(c);
        out.push({
          tid: tids.length - 1,
          depth: depth,
          name: nameOf(c),
          tag: (c.getAttribute('class') || '').trim().split(/\s+/)[0] || c.tagName.toLowerCase(),
          kids: c.children.length,
          swatch: hex(cs.backgroundColor),
          on: c === sel
        });
        if (depth < 12) walk(c, depth + 1);
      }
    })(document.body, 0);
    post({ dz: 'tree', nodes: out });
  }

  /** Apply declarations to the selection. A value of '' removes it. */
  function style(decls) {
    if (!sel || mode !== 'edit') return;
    Object.keys(decls || {}).forEach(function (k) {
      var v = decls[k];
      if (v === '' || v === null || v === undefined) sel.style.removeProperty(k);
      else sel.style.setProperty(k, String(v));
    });
    place();
    flush();
    post({ dz: 'selected', label: label(sel), rect: rectOf(sel),
           details: details(sel), props: props(sel) });
  }

  function rectOf(el) {
    var r = el.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height, svg: isSvg(el) };
  }

  function select(el) {
    sel = el;
    if (!el) {
      if (box) box.style.display = 'none';
      post({ dz: 'selected', label: '' });
      return;
    }
    ensureBox().style.display = 'block';
    // Handles only make sense where they do something: not while inspecting,
    // and not on an SVG node, which has no box to pull on.
    box.classList.toggle('inspect', mode === 'inspect');
    box.classList.toggle('svg', isSvg(el));
    place();
    post({
      dz: 'selected',
      label: label(el),
      rect: rectOf(el),
      details: details(el),
      props: props(el)
    });
  }

  // -- drag & resize --------------------------------------------------------
  /** The translate this editor previously appended, split from the rest. */
  function splitTransform(el) {
    var tf = (el.getAttribute('transform') || '').trim();
    var m = tf.match(/^(.*?)\s*translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)$/);
    if (m) return { base: m[1], x: parseFloat(m[2]), y: parseFloat(m[3]) };
    return { base: tf, x: 0, y: 0 };
  }

  function beginDrag(target, x, y) {
    if (!sel || mode !== 'edit') return;
    var handle = target && target.dataset ? target.dataset.dzHandle : null;
    var r = sel.getBoundingClientRect();
    if (isSvg(sel)) {
      var tf = splitTransform(sel);
      drag = {
        kind: 'move', svg: true, base: tf.base,
        startX: x, startY: y, left: tf.x, top: tf.y, w: r.width, h: r.height
      };
    } else {
      var s = getComputedStyle(sel);
      if (s.position === 'static') {
        // ours, to hang the drag on — remembered so it can be undone in the
        // markup we hand out and the document we serialise
        sel.style.position = 'relative';
        sel.setAttribute('data-dz-pos', '1');
      }
      drag = {
        kind: handle || 'move', svg: false,
        startX: x, startY: y,
        left: parseFloat(sel.style.left || '0') || 0,
        top: parseFloat(sel.style.top || '0') || 0,
        w: r.width, h: r.height
      };
    }
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onDrop);
  }

  function onGrab(e) {
    if (!sel || mode !== 'edit') return;
    e.preventDefault();
    e.stopPropagation();
    beginDrag(e.target, e.clientX, e.clientY);
  }

  function moveTo(x, y) {
    if (!drag || !sel) return;
    var dx = x - drag.startX;
    var dy = y - drag.startY;
    if (drag.svg) {
      var tx = drag.left + dx;
      var ty = drag.top + dy;
      sel.setAttribute(
        'transform',
        (drag.base ? drag.base + ' ' : '') + 'translate(' + tx + ' ' + ty + ')'
      );
      place();
      return;
    }
    // Once an offset is applied the position is load-bearing: left and top do
    // nothing on a static element, so serialising it without the position it
    // was given throws the move away. Drop the marker that would strip it —
    // this position belongs to the design now, not to the editor.
    if (drag.kind === 'move') {
      sel.style.left = (drag.left + dx) + 'px';
      sel.style.top = (drag.top + dy) + 'px';
      sel.removeAttribute('data-dz-pos');
    } else {
      // Resizing from a west or north edge moves the box as it shrinks, so the
      // offset has to travel with the size.
      if (drag.kind.indexOf('e') > -1) sel.style.width = Math.max(16, drag.w + dx) + 'px';
      if (drag.kind.indexOf('s') > -1) sel.style.height = Math.max(16, drag.h + dy) + 'px';
      if (drag.kind.indexOf('w') > -1) {
        sel.style.width = Math.max(16, drag.w - dx) + 'px';
        sel.style.left = (drag.left + dx) + 'px';
        sel.removeAttribute('data-dz-pos');
      }
      if (drag.kind.indexOf('n') > -1) {
        sel.style.height = Math.max(16, drag.h - dy) + 'px';
        sel.style.top = (drag.top + dy) + 'px';
        sel.removeAttribute('data-dz-pos');
      }
    }
    place();
  }

  function onMove(e) { moveTo(e.clientX, e.clientY); }

  function onDrop() {
    if (!drag) return;
    drag = null;
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onDrop);
    flush();
  }

  // -- pointer routing ------------------------------------------------------
  document.addEventListener('mouseover', function (e) {
    if ((mode !== 'inspect' && mode !== 'edit') || drag) return;
    var el = e.target;
    if (!editable(el)) return;
    el.setAttribute('data-dz-hover', '1');
  }, true);

  document.addEventListener('mouseout', function (e) {
    if (e.target.removeAttribute) e.target.removeAttribute('data-dz-hover');
  }, true);

  function handleClick(el, pageX, pageY) {
    if (mode === 'view') return;   // the design is just a page
    if (mode === 'comment') {
      var w = document.documentElement.scrollWidth;
      var h = document.documentElement.scrollHeight;
      post({ dz: 'comment', x: pageX / w, y: pageY / h });
      return;
    }
    if (!editable(el)) { select(null); return; }
    if (el === sel) return;   // clicking the selection keeps it
    // In edit mode a click selects — which is what makes it draggable and
    // resizable. Text is a double-click, the way every design tool does it.
    select(el);
  }

  function startTyping(el) {
    if (!editable(el) || mode !== 'edit') return;
    select(el);
    // SVG text can't be contenteditable, so it gets a real input laid over it
    // and its textContent written back. Same gesture, either way.
    if (isSvg(el)) return typeOverSvg(el);
    el.setAttribute('contenteditable', 'true');
    el.focus();
    post({ dz: 'typing' });
    el.addEventListener('blur', function once() {
      el.removeAttribute('contenteditable');
      el.removeEventListener('blur', once);
      post({ dz: 'typed' });
      flush();
    });
  }

  function typeOverSvg(el) {
    var r = el.getBoundingClientRect();
    var s = getComputedStyle(el);
    var input = mark(document.createElement('input'));
    input.className = 'dz-svg-input';
    input.value = el.textContent || '';
    input.style.cssText =
      'position:absolute;z-index:2147483001;left:' + (r.left + scrollX - 4) +
      'px;top:' + (r.top + scrollY - 3) + 'px;min-width:' + Math.max(60, r.width + 16) +
      'px;height:' + (r.height + 6) + 'px;font:' + s.font + ';color:' + s.fill +
      ';background:#fff;border:1px solid #0071E3;border-radius:3px;padding:0 4px';
    document.body.appendChild(input);
    input.focus();
    input.select();
    post({ dz: 'typing' });

    function finish(keep) {
      if (keep) el.textContent = input.value;
      input.remove();
      post({ dz: 'typed' });
      place();
      if (keep) flush();
    }
    input.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', function () { finish(true); });
  }

  document.addEventListener('dblclick', function (e) {
    if (mode !== 'edit') return;
    e.preventDefault();
    e.stopPropagation();
    startTyping(target(e.target, e.clientX, e.clientY));
  }, true);

  document.addEventListener('click', function (e) {
    if (mode === 'view') return;   // links, buttons and scripts behave normally
    var el = target(e.target, e.clientX, e.clientY);
    if (mode === 'comment' || editable(el)) {
      e.preventDefault();
      e.stopPropagation();
    }
    handleClick(el, e.pageX, e.pageY);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (mode === 'inspect' && e.key === 'Escape') { select(null); return; }
    if (!sel || mode !== 'edit') return;
    if (document.querySelector('[contenteditable="true"]')) return;
    var step = e.shiftKey ? 10 : 1;
    var map = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (map[e.key]) {
      e.preventDefault();
      if (isSvg(sel)) {
        var tf = splitTransform(sel);
        sel.setAttribute(
          'transform',
          (tf.base ? tf.base + ' ' : '') +
            'translate(' + (tf.x + map[e.key][0]) + ' ' + (tf.y + map[e.key][1]) + ')'
        );
      } else {
        if (getComputedStyle(sel).position === 'static') sel.style.position = 'relative';
        sel.style.left = ((parseFloat(sel.style.left || '0') || 0) + map[e.key][0]) + 'px';
        sel.style.top = ((parseFloat(sel.style.top || '0') || 0) + map[e.key][1]) + 'px';
        sel.removeAttribute('data-dz-pos');   // nudged: the position is the design's
      }
      place();
      flush();
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      var gone = sel;
      select(null);
      gone.remove();
      flush();
    } else if (e.key === 'Escape') {
      select(null);
    }
  });

  addEventListener('scroll', place, true);
  addEventListener('resize', place);

  // -- alignment ------------------------------------------------------------
  function align(how) {
    if (!sel || mode !== 'edit') return;
    var across = (how === 'left' || how === 'center' || how === 'right');
    var parent = sel.parentElement;
    var ps = parent ? getComputedStyle(parent) : null;
    var own = getComputedStyle(sel);
    var box = ps ? ps.display : '';
    var column = ps ? (ps.flexDirection || 'row').indexOf('column') === 0 : false;
    var pick = function (a, b, c) {
      return how === 'left' || how === 'top' ? a
        : (how === 'center' || how === 'middle' ? b : c);
    };

    // What "align" means depends on what the element is and what it sits in.
    // Setting two margins and hoping only works where the container has free
    // space to give: a button in a content-sized flex row has none, and every
    // click on the toolbar moved nothing at all.
    if (across) {
      if (box === 'grid' || box === 'inline-grid') {
        sel.style.justifySelf = pick('start', 'center', 'end');
      } else if (box === 'flex' || box === 'inline-flex') {
        if (column) {
          sel.style.alignSelf = pick('flex-start', 'center', 'flex-end');
        } else {
          // Along a flex row auto margins eat the free space — so long as the
          // item is not itself growing to take all of it.
          if (own.flexGrow !== '0') sel.style.flexGrow = '0';
          sel.style.marginLeft = how === 'left' ? '0px' : 'auto';
          sel.style.marginRight = how === 'right' ? '0px' : 'auto';
        }
      } else if (own.display.indexOf('inline') === 0) {
        // A run of text sits in a line, and the line is the parent's to set.
        if (parent) parent.style.textAlign = pick('left', 'center', 'right');
      } else if (!sel.children.length && sel.textContent.trim()) {
        // A heading or paragraph across the full width: move the words, not
        // the box. Shrinking it to its text would reflow everything below.
        sel.style.textAlign = pick('left', 'center', 'right');
      } else {
        // A block fills its parent, so there is nothing to move until it is
        // asked to be only as wide as it needs. Measure against the parent's
        // content box — its border box counts padding the child never had.
        var room = parent
          ? parent.clientWidth - parseFloat(ps.paddingLeft || 0) - parseFloat(ps.paddingRight || 0)
          : 0;
        if (parent && sel.getBoundingClientRect().width >= room - 1 && !sel.style.width) {
          sel.style.width = 'fit-content';
        }
        sel.style.marginLeft = how === 'left' ? '0px' : 'auto';
        sel.style.marginRight = how === 'right' ? '0px' : 'auto';
      }
    } else {
      if (box === 'flex' || box === 'inline-flex') {
        if (column) {
          sel.style.marginTop = how === 'top' ? '0px' : 'auto';
          sel.style.marginBottom = how === 'bottom' ? '0px' : 'auto';
        } else {
          sel.style.alignSelf = pick('flex-start', 'center', 'flex-end');
        }
      } else if (box === 'grid' || box === 'inline-grid') {
        sel.style.alignSelf = pick('start', 'center', 'end');
      } else {
        sel.style.verticalAlign = pick('top', 'middle', 'bottom');
      }
    }
    place();
    flush();
  }

  // -- comment pins ---------------------------------------------------------
  function drawPins(pins) {
    if (!pinLayer) {
      pinLayer = mark(document.createElement('div'));
      pinLayer.id = 'dz-pins';
      document.body.appendChild(pinLayer);
    }
    pinLayer.textContent = '';
    var w = document.documentElement.scrollWidth;
    var h = document.documentElement.scrollHeight;
    pinLayer.style.height = h + 'px';
    (pins || []).forEach(function (p, i) {
      var el = mark(document.createElement('div'));
      el.className = 'dz-pin';
      el.style.left = (p.x * w) + 'px';
      el.style.top = (p.y * h) + 'px';
      el.textContent = (i + 1) + '. ' + p.text;
      pinLayer.appendChild(el);
    });
  }

  // -- serialise ------------------------------------------------------------
  var pending = null;
  function flush() {
    clearTimeout(pending);
    pending = setTimeout(function () {
      try {
        var clone = document.documentElement.cloneNode(true);
        clone.querySelectorAll('[data-dz]').forEach(function (n) { n.remove(); });
        clone.querySelectorAll('[data-dz-hover]').forEach(function (n) {
          n.removeAttribute('data-dz-hover');
        });
        clone.querySelectorAll('[data-dz-handle]').forEach(function (n) {
          n.removeAttribute('data-dz-handle');
        });
        clone.querySelectorAll('[data-dz-pos]').forEach(function (n) {
          n.style.position = '';
          n.removeAttribute('data-dz-pos');
          if (!n.getAttribute('style')) n.removeAttribute('style');
        });
        clone.querySelectorAll('[contenteditable]').forEach(function (n) {
          n.removeAttribute('contenteditable');
        });
        post({ dz: 'html', html: '<!DOCTYPE html>\n' + clone.outerHTML });
      } catch {
        // A design that mangles its own DOM shouldn't take the panel down with
        // it; the next edit will try again.
      }
    }, 220);
  }

  // Some embeddings hand the click to the iframe element in the parent rather
  // than routing it into this document. The parent forwards those as
  // coordinates; everything below is the same code path a native event takes.
  function forwarded(m) {
    var el = document.elementFromPoint(m.x, m.y);
    if (m.kind === 'click') {
      handleClick(target(el, m.x, m.y), m.x + scrollX, m.y + scrollY);
    } else if (m.kind === 'dblclick') {
      startTyping(target(el, m.x, m.y));
    } else if (m.kind === 'down') {
      if (mode !== 'edit') return;
      if (el && el.closest && el.closest('#dz-box')) {
        beginDrag(el, m.x, m.y);          // a handle or the move surface
      } else if (editable(el)) {
        select(el);
        beginDrag(null, m.x, m.y);        // press-and-drag in one gesture
      }
    } else if (m.kind === 'move') {
      moveTo(m.x, m.y);
    } else if (m.kind === 'up') {
      onDrop();
    }
  }

  addEventListener('message', function (e) {
    var m = e.data || {};
    if (m.dz === 'mode') {
      mode = m.mode || 'view';
      document.body.classList.toggle('dz-comment', mode === 'comment');
      // A selection made under one tool means nothing under the next.
      select(null);
    } else if (m.dz === 'align') { align(m.align); }
    else if (m.dz === 'delete') {
      if (sel && mode === 'edit') { var g = sel; select(null); g.remove(); flush(); }
    }
    else if (m.dz === 'pins') { drawPins(m.pins); }
    else if (m.dz === 'deselect') { select(null); }
    else if (m.dz === 'flush') { flush(); }
    else if (m.dz === 'pointer') { forwarded(m); }
    else if (m.dz === 'tweak') { applyTweak(m.tweakVar, m.tweakValue); }
    else if (m.dz === 'grab') {
      // Hand back exactly what is selected, with nothing of ours in it.
      if (!sel) { post({ dz: 'grabbed', html: '' }); return; }
      var copy = sel.cloneNode(true);
      copy.querySelectorAll('[data-dz]').forEach(function (n) { n.remove(); });
      if (copy.getAttribute('data-dz-pos')) {
        copy.style.position = '';
        copy.removeAttribute('data-dz-pos');
        if (!copy.getAttribute('style')) copy.removeAttribute('style');
      }
      copy.removeAttribute('data-dz-hover');
      copy.removeAttribute('contenteditable');
      copy.querySelectorAll('[data-dz-hover],[contenteditable]').forEach(function (n) {
        n.removeAttribute('data-dz-hover'); n.removeAttribute('contenteditable');
      });
      post({ dz: 'grabbed', html: copy.outerHTML, label: label(sel), path: pathTo(sel) });
    }
    else if (m.dz === 'style') { style(m.decls); }
    else if (m.dz === 'text') {
      if (sel && mode === 'edit' && sel.children.length === 0) {
        sel.textContent = m.text || '';
        place(); flush();
      }
    }
    else if (m.dz === 'attr') {
      if (sel && mode === 'edit' && m.name) {
        if (m.value === '' || m.value === null) sel.removeAttribute(m.name);
        else sel.setAttribute(m.name, m.value);
        place(); flush();
      }
    }
    else if (m.dz === 'css') {
      // The Code tab: one declaration per line, @name for an attribute.
      if (sel && mode === 'edit') {
        sel.removeAttribute('style');
        (m.text || '').split(/[\n;]+/).forEach(function (line) {
          var t = line.trim();
          if (!t) return;
          var at = t.indexOf(':');
          if (at < 1) return;
          var k = t.slice(0, at).trim(), v = t.slice(at + 1).trim();
          if (k.charAt(0) === '@') sel.setAttribute(k.slice(1), v);
          else sel.style.setProperty(k, v);
        });
        place(); flush();
        post({ dz: 'selected', label: label(sel), rect: rectOf(sel),
               details: details(sel), props: props(sel) });
      }
    }
    else if (m.dz === 'tree') { tree(); }
    else if (m.dz === 'insert') { insert(m.kind); }
    else if (m.dz === 'pick') {
      var want = tids[m.tid];
      if (want && mode === 'edit') {
        select(want);
        // Only scroll if it is actually off screen — the canvas is scaled, and
        // scrolling for something already visible throws the sheet out of view.
        var r = want.getBoundingClientRect();
        if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) {
          want.scrollIntoView({ block: 'center', inline: 'nearest' });
        }
      }
    }
    else if (m.dz === 'replace') {
      if (!sel || !m.html) return;
      var holder = document.createElement('div');
      holder.innerHTML = m.html;
      var fresh = holder.firstElementChild;
      if (!fresh) return;
      var old = sel;
      select(null);
      old.parentNode.replaceChild(fresh, old);
      select(fresh);
      flush();
    }
  });

  // -- tweaks ---------------------------------------------------------------
  // A design can declare the knobs worth turning. Anything it doesn't declare
  // is inferred from its own :root custom properties, so even a document that
  // never heard of this panel is still tunable.
  function readTweaks() {
    var declared = document.getElementById('tweaks');
    if (declared) {
      try {
        var list = JSON.parse(declared.textContent || '[]');
        if (list && list.length) return list;
      } catch (err) { /* fall through to inference */ }
    }
    var named = [];
    var rest = [];
    var seen = {};
    // A variable with a role in its name is a knob worth offering; a numbered
    // ramp step is scaffolding. Show the roles, and only fall back to the ramp
    // when a design has nothing else.
    var roles = /(accent|brand|primary|secondary|highlight|ink|text|surface|background|bg|paper|ground|border|rule|link)/i;
    for (var i = 0; i < document.styleSheets.length; i++) {
      var sheet = document.styleSheets[i];
      var rules;
      try { rules = sheet.cssRules; } catch (err) { continue; }
      for (var j = 0; rules && j < rules.length; j++) {
        var rule = rules[j];
        if (!rule.selectorText || rule.selectorText.indexOf(':root') === -1) continue;
        for (var k = 0; k < rule.style.length; k++) {
          var name = rule.style[k];
          if (name.indexOf('--') !== 0 || seen[name]) continue;
          var value = rule.style.getPropertyValue(name).trim();
          if (!/^(#|rgb|hsl)/i.test(value)) continue;   // colours are the useful knobs
          seen[name] = 1;
          var knob = { name: name.replace(/^--/, ''), type: 'color', var: name, value: value };
          if (roles.test(name) && !/\d/.test(name)) named.push(knob);
          else rest.push(knob);
        }
      }
    }
    return (named.length ? named : rest).slice(0, 8);
  }

  function applyTweak(name, value) {
    if (!name) return;
    document.documentElement.style.setProperty(name, value);
    flush();
  }

  window.addEventListener('message', function (e) {
    if (e && e.data && e.data.dz === 'ping') post({ dz: 'pong' });
  });

  // A design whose own script throws renders empty and looks like Compass
  // losing the file. Say what actually happened.
  window.addEventListener('error', function (e) {
    post({ dz: 'pageerror', message: String((e && e.message) || 'script error') });
  });

  post({ dz: 'ready' });
  post({ dz: 'tweaks', tweaks: readTweaks() });
})();
`;
