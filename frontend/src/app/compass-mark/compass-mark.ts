import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/**
 * Compass mark — the brand logo. A crisp geometric instrument: a fine bezel
 * ring with cardinal ticks and a two-tone diamond needle (brass north, muted
 * south). Pure inline SVG driven by CSS custom properties, so it inherits the
 * theme and stays razor-sharp at any size — no canvas, no blur.
 *
 * When `active` (a turn is streaming) the outer sweep ring rotates slowly and
 * precisely, like an instrument taking a reading. Respects reduced-motion via
 * the global rule in styles.css.
 */
@Component({
  selector: 'app-compass-mark',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      [attr.width]="size"
      [attr.height]="size"
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
      class="mark"
      [class.active]="active"
    >
      <!-- bezel -->
      <circle cx="50" cy="50" r="46" stroke="var(--bezel, currentColor)"
        stroke-opacity="0.28" stroke-width="1.5" />
      <circle cx="50" cy="50" r="39" stroke="var(--bezel, currentColor)"
        stroke-opacity="0.16" stroke-width="1" />

      <!-- rotating sweep ring (animated when active) -->
      <g class="sweep">
        <path d="M50 6 A44 44 0 0 1 94 50" stroke="var(--accent)"
          stroke-width="2" stroke-linecap="round" stroke-opacity="0.9" />
        <circle cx="50" cy="6" r="2.1" fill="var(--accent)" />
      </g>

      <!-- cardinal ticks -->
      <g stroke="var(--bezel, currentColor)" stroke-opacity="0.4" stroke-width="1.4">
        <line x1="50" y1="9" x2="50" y2="15" />
        <line x1="50" y1="85" x2="50" y2="91" />
        <line x1="9" y1="50" x2="15" y2="50" />
        <line x1="85" y1="50" x2="91" y2="50" />
      </g>

      <!-- needle: N brass, S muted -->
      <path d="M50 16 L57 50 L50 50 Z" fill="var(--accent)" />
      <path d="M50 16 L43 50 L50 50 Z" fill="var(--accent-strong)" />
      <path d="M50 84 L57 50 L50 50 Z" fill="var(--needle-s, currentColor)"
        fill-opacity="0.34" />
      <path d="M50 84 L43 50 L50 50 Z" fill="var(--needle-s, currentColor)"
        fill-opacity="0.22" />

      <!-- hub -->
      <circle cx="50" cy="50" r="4" fill="var(--hub, currentColor)" />
      <circle cx="50" cy="50" r="4" stroke="var(--accent)" stroke-width="1.2" />
      <circle cx="50" cy="50" r="1.5" fill="var(--accent)" />
    </svg>
  `,
  styles: [
    `
      :host { display: inline-block; line-height: 0; color: var(--ink); }
      .mark { display: block; }
      .sweep { transform-origin: 50px 50px; }
      .mark.active .sweep { animation: sweep 2.8s linear infinite; }
      @keyframes sweep { to { transform: rotate(360deg); } }
    `,
  ],
})
export class CompassMark {
  @Input() size = 40;
  @Input() active = false;
}
