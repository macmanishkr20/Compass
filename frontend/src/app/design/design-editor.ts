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
    | 'tweak' | 'ping' | 'grab' | 'replace';
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
  kind?: 'down' | 'move' | 'up' | 'click' | 'dblclick';
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
    | 'tweaks' | 'pong' | 'pageerror' | 'grabbed';
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
    var r = el.getBoundingClientRect();
    post({
      dz: 'selected',
      label: label(el),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height, svg: isSvg(el) },
      details: details(el)
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
    if (drag.kind === 'move') {
      sel.style.left = (drag.left + dx) + 'px';
      sel.style.top = (drag.top + dy) + 'px';
    } else {
      // Resizing from a west or north edge moves the box as it shrinks, so the
      // offset has to travel with the size.
      if (drag.kind.indexOf('e') > -1) sel.style.width = Math.max(16, drag.w + dx) + 'px';
      if (drag.kind.indexOf('s') > -1) sel.style.height = Math.max(16, drag.h + dy) + 'px';
      if (drag.kind.indexOf('w') > -1) {
        sel.style.width = Math.max(16, drag.w - dx) + 'px';
        sel.style.left = (drag.left + dx) + 'px';
      }
      if (drag.kind.indexOf('n') > -1) {
        sel.style.height = Math.max(16, drag.h - dy) + 'px';
        sel.style.top = (drag.top + dy) + 'px';
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
