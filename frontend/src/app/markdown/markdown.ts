import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { ArtifactService } from '../artifact.service';
import { ImageActionsService } from '../image-actions.service';
import { Artifact } from '../models';

interface ProseSeg {
  type: 'prose';
  html: string;
}
interface CodeSeg {
  type: 'code';
  lang: string;
  code: string;
}
interface ArtifactSeg {
  type: 'artifact';
  artifact: Artifact;
}
/** A video rendered by `make_video`, linked on a line of its own. */
interface VideoSeg {
  type: 'video';
  url: string;
  name: string;
}
/** A picture this server drew, on a line of its own, so it can carry the
 *  two things somebody wants from a picture: keep it, or change it. */
interface PicSeg {
  type: 'pic';
  url: string;
  alt: string;
}
type Seg = ProseSeg | CodeSeg | ArtifactSeg | VideoSeg | PicSeg;

/**
 * A link that is a playable file this server produced. Deliberately narrow:
 * only this origin's chat-media route, only the container types a browser
 * plays. Anything else stays an ordinary link, which is the safe failure —
 * a link that should have been a player is a click away from working, and a
 * player pointed at someone else's URL is not something a chat message
 * should be able to conjure.
 */
const MEDIA_LINE =
  /^\s*\[([^\]]+)\]\((\/v1\/chat\/sessions\/[\w-]+\/media\/[^)\s]+\.(?:mp4|webm|m4v|mov))\)\s*$/i;

/**
 * A picture Compass drew, written on a line of its own. As narrow as the
 * video rule above and for the same reason: only this origin's generated
 * route, only the hex id the server mints. Anything else stays an ordinary
 * inline image, which is the safe failure — Download and Edit are offered
 * for pictures this server can actually act on, and an Edit button over
 * somebody else's URL would be a promise it cannot keep.
 */
const DRAWN_LINE =
  /^\s*!\[([^\]]*)\]\((\/v1\/media\/generated\/[0-9a-f]{32}\.png)\)\s*$/i;

/**
 * The host a citation names, or "" when the link is ordinary prose.
 *
 * Models cite by writing the bare domain as the label — `[timeanddate.com]
 * (https://…)`. That is the whole signal, and it is a narrow one on purpose:
 * a label with words in it is a sentence the person is meant to read, and
 * turning it into a chip would eat the sentence.
 */
function citeHost(label: string, url: string): string {
  const text = label.trim().replace(/^www\./, '');
  // A bare host: letters, digits, hyphens and dots, ending in a real TLD.
  if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(text)) return '';
  if (!/^https?:\/\//i.test(url)) return '';
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    // The label has to actually be the link's host, or something close to it
    // — otherwise it is a domain being talked about, not a source.
    return host === text || host.endsWith('.' + text) || text.endsWith('.' + host)
      ? host
      : '';
  } catch {
    return '';
  }
}

/**
 * Dependency-free, streaming-safe Markdown renderer for assistant messages.
 * Handles fenced code blocks (with a language chip + copy button), headings,
 * bold/italic, inline code, links, and bullet/numbered lists. HTML is escaped
 * before any formatting, and prose is bound through Angular's sanitizer, so
 * model output can never inject markup.
 */
@Component({
  selector: 'app-markdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @for (seg of segments(); track $index) {
      @if (seg.type === 'code') {
        @let long = lineCount(asCode(seg).code) > 16;
        @let open = expanded().has($index);
        <div class="code-block">
          <div class="code-head">
            <span class="code-lang">{{ asCode(seg).lang || 'code' }}</span>
            <button class="code-copy" type="button" (click)="copy(asCode(seg).code, $index)">
              @if (copiedIdx() === $index) { ✓ Copied } @else { Copy }
            </button>
          </div>
          <div class="code-body" [class.collapsed]="long && !open">
            <pre><code>{{ asCode(seg).code }}</code></pre>
          </div>
          @if (long) {
            <button class="code-more" type="button" (click)="toggleExpand($index)">
              {{ open ? 'Show less' : 'Show more (' + lineCount(asCode(seg).code) + ' lines)' }}
            </button>
          }
        </div>
      } @else if (seg.type === 'artifact') {
        <button class="artifact-card" type="button"
          [class.active]="artifacts.active()?.id === asArt(seg).artifact.id"
          (click)="openArtifact(asArt(seg).artifact)">
          <span class="artifact-thumb">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 5a1 1 0 011-1h14a1 1 0 011 1v14a1 1 0 01-1 1H5a1 1 0 01-1-1z"/><path d="M4 9h16" stroke-linecap="round"/><path d="M8 13h5M8 16h8" stroke-linecap="round"/></svg>
          </span>
          <span class="artifact-meta">
            <span class="artifact-title">{{ asArt(seg).artifact.title }}</span>
            <span class="artifact-sub">{{ asArt(seg).artifact.kind === 'azure' ? 'Azure architecture · click to open' : asArt(seg).artifact.kind === 'drawio' ? 'Azure diagram · open in draw.io' : asArt(seg).artifact.kind === 'mermaid' ? 'Diagram · click to open' : asArt(seg).artifact.kind === 'svg' ? 'SVG image · click to open' : 'Interactive document · click to open' }}</span>
          </span>
          <svg class="artifact-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      } @else if (seg.type === 'pic') {
        <figure class="md-pic">
          <img [src]="asPic(seg).url" [alt]="asPic(seg).alt" loading="lazy" />
          <figcaption>
            <!-- The download attribute on an anchor to this origin saves
                 rather than navigates, which is what a browser would
                 otherwise do with a 2MB PNG: display it again. -->
            <a [href]="asPic(seg).url" [download]="fileName(asPic(seg).url)">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v10m0 0l-4-4m4 4l4-4M5 19h14"/></svg>
              Download
            </a>
            <button type="button" (click)="editPicture(asPic(seg).url)">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16.5 3.5l4 4L8 20l-4.5 1L4.5 16.5z"/></svg>
              Edit
            </button>
          </figcaption>
        </figure>
      } @else if (seg.type === 'video') {
        <figure class="md-video">
          <!-- Bound, not written into innerHTML: the sanitizer's element list
               is not a promise, and a property binding renders a real player
               every time. -->
          <video [src]="asVideo(seg).url" controls playsinline preload="metadata"></video>
          <figcaption>
            <span class="md-video-name">{{ asVideo(seg).name }}</span>
            <a [href]="asVideo(seg).url" [download]="asVideo(seg).name">Download</a>
          </figcaption>
        </figure>
      } @else {
        <div class="prose" [innerHTML]="asProse(seg).html"></div>
      }
    }
  `,
  styleUrl: './markdown.css',
})
export class Markdown {
  readonly text = input.required<string>();
  readonly copiedIdx = signal<number | null>(null);
  readonly expanded = signal<Set<number>>(new Set());
  readonly artifacts = inject(ArtifactService);
  private readonly imageActions = inject(ImageActionsService);

  readonly segments = computed<Seg[]>(() => this.parse(this.text()));

  lineCount(code: string): number {
    return code.split('\n').length;
  }
  toggleExpand(i: number): void {
    const s = new Set(this.expanded());
    s.has(i) ? s.delete(i) : s.add(i);
    this.expanded.set(s);
  }

  asCode = (s: Seg) => s as CodeSeg;
  asProse = (s: Seg) => s as ProseSeg;
  asArt = (s: Seg) => s as ArtifactSeg;
  asVideo = (s: Seg) => s as VideoSeg;
  asPic = (s: Seg) => s as PicSeg;

  /** What the browser should call the file it saves. The stored name is a
   *  32-character hex id, which is a fine key and a poor filename. */
  fileName(url: string): string {
    return 'compass-image-' + (url.split('/').pop() || 'image.png').slice(0, 8) + '.png';
  }

  /** Hand the picture to whichever composer is on screen; see
   *  ImageActionsService for why it goes through a signal. */
  editPicture(url: string): void {
    this.imageActions.requestEdit(url);
  }

  openArtifact(a: Artifact): void {
    this.artifacts.open(a);
  }

  async copy(code: string, idx: number): Promise<void> {
    let ok = false;
    try {
      await navigator.clipboard.writeText(code);
      ok = true;
    } catch {
      ok = this.execCommandCopy(code); // fallback for non-secure / iframe contexts
    }
    if (ok) {
      this.copiedIdx.set(idx);
      setTimeout(() => this.copiedIdx.set(null), 1400);
    }
  }

  private execCommandCopy(text: string): boolean {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }

  // -- parsing -------------------------------------------------------------

  private parse(src: string): Seg[] {
    const segs: Seg[] = [];
    const fence = /```([\w+-]*)\n?([\s\S]*?)```/g;
    let last = 0;
    let m: RegExpExecArray | null;
    while ((m = fence.exec(src))) {
      if (m.index > last) this.pushProse(segs, src.slice(last, m.index));
      const lang = m[1] || '';
      const code = m[2].replace(/\n$/, '');
      // A completed fence that looks like a full document becomes an artifact
      // card; everything else is a normal code block. (Only closed fences —
      // the trailing unclosed one below stays code while it streams.)
      const kind = ArtifactService.classify(lang, code);
      if (kind) {
        segs.push({
          type: 'artifact',
          artifact: {
            id: ArtifactService.idFor(code),
            kind,
            title: ArtifactService.titleFor(kind, code),
            code,
          },
        });
      } else {
        segs.push({ type: 'code', lang, code });
      }
      last = fence.lastIndex;
    }
    // Trailing content, including an unclosed fence still streaming.
    const rest = src.slice(last);
    const open = rest.indexOf('```');
    if (open >= 0) {
      if (open > 0) this.pushProse(segs, rest.slice(0, open));
      const after = rest.slice(open + 3);
      const nl = after.indexOf('\n');
      const lang = (nl >= 0 ? after.slice(0, nl) : after).trim();
      const code = nl >= 0 ? after.slice(nl + 1) : '';
      segs.push({ type: 'code', lang, code });
    } else if (rest) {
      this.pushProse(segs, rest);
    }
    return segs;
  }

  private pushProse(segs: Seg[], text: string): void {
    // A link to a rendered video, alone on its line, becomes a player; the
    // prose around it is unaffected. Done here rather than in `inline()`
    // because a player is a block — inline replacement would bury a 9:16
    // video inside a paragraph.
    let buffer: string[] = [];
    const flush = () => {
      const html = this.renderProse(buffer.join('\n'));
      if (html.trim()) segs.push({ type: 'prose', html });
      buffer = [];
    };
    for (const line of text.split('\n')) {
      const match = MEDIA_LINE.exec(line);
      const drawn = match ? null : DRAWN_LINE.exec(line);
      if (match) {
        flush();
        segs.push({ type: 'video', url: match[2], name: match[1] });
      } else if (drawn) {
        flush();
        segs.push({ type: 'pic', url: drawn[2], alt: drawn[1] });
      } else {
        buffer.push(line);
      }
    }
    flush();
  }


  private escape(s: string): string {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  private inline(text: string): string {
    // The screenshot tool posts its image automatically; drop any stray
    // screenshot://<id> token the model echoes so it never shows as text.
    let t = this.escape(text.replace(/\[?screenshot:\/\/[a-f0-9]+\]?/gi, '').trimEnd());
    // inline code first — protect its contents from other rules
    const codes: string[] = [];
    t = t.replace(/`([^`]+)`/g, (_, c) => {
      codes.push(c);
      return ` ${codes.length - 1} `;
    });
    // images ![alt](url) — allow data:, /v1/, http(s) only
    t = t.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
      /^(data:image\/|https?:\/\/|\/v1\/)/.test(url)
        ? `<img class="md-img" src="${url}" alt="${alt}" loading="lazy" />`
        : m,
    );
    // links [label](url) — a citation becomes a chip, everything else an
    // ordinary link. A citation is recognised by its label being a bare host,
    // which is how a model writes one: [timeanddate.com](https://…). Anything
    // with real words in the label is prose and stays prose.
    t = t.replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      (whole: string, label: string, url: string) => {
        const host = citeHost(label, url);
        if (!host) return `<a href="${url}" target="_blank" rel="noopener">${label}</a>`;
        // The icon is fetched by the server, so the browser never contacts
        // the cited site.
        //
        // The fallback is done with layout rather than an onerror handler,
        // because this HTML goes through Angular's sanitizer and every event
        // attribute is stripped from it — a handler here would look right in
        // the source and never fire. So the letter is real content and the
        // image is laid over it: when the fetch 404s the image has no size
        // and the letter shows through.
        const icon = `/v1/chat/favicon?url=${encodeURIComponent(url)}`;
        return (
          `<a class="md-cite" href="${url}" target="_blank" rel="noopener" title="${host}">` +
          `<span class="md-cite-ico"><i>${host[0].toUpperCase()}</i>` +
          `<img src="${icon}" alt="" loading="lazy" /></span>` +
          `<span class="md-cite-host">${host}</span></a>`
        );
      },
    );
    // A citation the model wrapped in its own brackets — "… breeze.
    // (timeanddate.com)" — keeps the brackets around a chip that no longer
    // needs them. Only a pair holding exactly one chip and nothing else is
    // removed, so "(see timeanddate.com and the IMD page)" is left intact.
    t = t.replace(/\(\s*(<a class="md-cite"[\s\S]*?<\/a>)\s*\)/g, '$1');
    // bold, then italic
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>');
    t = t.replace(/(^|[^_])_([^_]+)_/g, '$1<em>$2</em>');
    // restore inline code
    t = t.replace(/ (\d+) /g, (_, i) => `<code>${codes[+i]}</code>`);
    return t;
  }

  private renderProse(src: string): string {
    const lines = src.split('\n');
    let html = '';
    let list: 'ul' | 'ol' | null = null;
    let para: string[] = [];
    const flushPara = () => {
      if (para.length) {
        html += `<p>${this.inline(para.join(' '))}</p>`;
        para = [];
      }
    };
    const flushList = () => {
      if (list) {
        html += `</${list}>`;
        list = null;
      }
    };
    // A table is the one block that cannot be decided from a single line: a
    // row of pipes is only a table once the dashed separator under it arrives.
    // So the walk is indexed, and a table is consumed whole.
    const isRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
    const isSep = (l: string) => /^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$/.test(l);
    const cellsOf = (l: string) =>
      l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
    const alignOf = (spec: string) => {
      const left = spec.startsWith(':');
      const right = spec.endsWith(':');
      if (left && right) return ' style="text-align:center"';
      if (right) return ' style="text-align:right"';
      return '';
    };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (!line.trim()) {
        flushPara();
        flushList();
        continue;
      }
      // Until the separator has streamed in, the row below falls through and
      // renders as ordinary text; it becomes a table on the next keystroke.
      if (isRow(line) && i + 1 < lines.length && isSep(lines[i + 1])) {
        flushPara();
        flushList();
        const head = cellsOf(line);
        const aligns = cellsOf(lines[i + 1]).map(alignOf);
        let body = '';
        let j = i + 2;
        for (; j < lines.length && isRow(lines[j]); j++) {
          const row = cellsOf(lines[j]);
          body += '<tr>' + head
            .map((_, c) => `<td${aligns[c] || ''}>${this.inline(row[c] ?? '')}</td>`)
            .join('') + '</tr>';
        }
        html +=
          '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
          head.map((h, c) => `<th${aligns[c] || ''}>${this.inline(h)}</th>`).join('') +
          '</tr></thead><tbody>' + body + '</tbody></table></div>';
        i = j - 1;
        continue;
      }
      let m: RegExpExecArray | null;
      if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
        flushPara();
        flushList();
        const lvl = Math.min(6, m[1].length + 2); // # -> h3, ## -> h4 …
        html += `<h${lvl}>${this.inline(m[2])}</h${lvl}>`;
      } else if ((m = /^\s*[-*]\s+(.*)$/.exec(line))) {
        flushPara();
        if (list !== 'ul') {
          flushList();
          html += '<ul>';
          list = 'ul';
        }
        html += `<li>${this.inline(m[1])}</li>`;
      } else if ((m = /^\s*\d+\.\s+(.*)$/.exec(line))) {
        flushPara();
        if (list !== 'ol') {
          flushList();
          html += '<ol>';
          list = 'ol';
        }
        html += `<li>${this.inline(m[1])}</li>`;
      } else {
        flushList();
        para.push(line.trim());
      }
    }
    flushPara();
    flushList();
    return html;
  }
}
