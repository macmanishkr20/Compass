import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/**
 * Radar-sweep loading icon — on-brand for a navigation instrument: a coral
 * beam sweeps the dial, an echo ring pings outward, and a contact blip
 * flashes as the beam passes it. Everything is keyed to one 2s revolution so
 * the motion reads as a single instrument taking bearings, not three random
 * animations.
 *
 * Motion is driven purely by [active]: when the response finishes or is
 * stopped, the beam freezes and the pings vanish — state and animation can
 * never disagree. prefers-reduced-motion renders it static.
 */
@Component({
  selector: 'app-loading-radar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      class="radar"
      [class.active]="active"
      [attr.width]="size"
      [attr.height]="size"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="beamGrad" x1="12" y1="12" x2="19" y2="5"
          gradientUnits="userSpaceOnUse">
          <stop offset="0" stop-color="var(--accent)" stop-opacity="0.45" />
          <stop offset="1" stop-color="var(--accent)" stop-opacity="0" />
        </linearGradient>
      </defs>

      <!-- dial -->
      <circle cx="12" cy="12" r="10" stroke="var(--accent)" stroke-opacity="0.28"
        stroke-width="1.4" />
      <circle cx="12" cy="12" r="5.5" stroke="var(--accent)" stroke-opacity="0.14"
        stroke-width="1" />
      <!-- cardinal ticks -->
      <g stroke="var(--accent)" stroke-opacity="0.4" stroke-width="1.2"
        stroke-linecap="round">
        <line x1="12" y1="1.2" x2="12" y2="3.2" />
        <line x1="12" y1="20.8" x2="12" y2="22.8" />
        <line x1="1.2" y1="12" x2="3.2" y2="12" />
        <line x1="20.8" y1="12" x2="22.8" y2="12" />
      </g>

      <!-- echo ping (expands + fades each revolution) -->
      <circle class="ping" cx="12" cy="12" r="9" stroke="var(--accent)"
        stroke-width="1.2" />

      <!-- rotating beam: trailing wedge + leading arm -->
      <g class="beam">
        <path d="M12 12 L12 2 A10 10 0 0 1 19.07 4.93 Z" fill="url(#beamGrad)" />
        <line x1="12" y1="12" x2="12" y2="2.4" stroke="var(--accent)"
          stroke-width="1.7" stroke-linecap="round" />
      </g>

      <!-- contact blip at ~45°, flashes as the beam passes -->
      <circle class="blip" cx="16.6" cy="7.4" r="1.5" fill="var(--accent)" />

      <!-- hub -->
      <circle cx="12" cy="12" r="1.6" fill="var(--accent)" />
    </svg>
  `,
  styles: [
    `
      :host { display: inline-grid; place-items: center; line-height: 0; }
      .radar { display: block; }
      .beam, .ping {
        transform-box: view-box;
        transform-origin: 50% 50%;
      }
      /* Static (finished/stopped): beam frozen at the blip, no pings. */
      .beam { transform: rotate(45deg); }
      .ping { opacity: 0; }
      .blip { opacity: 0.85; }

      .radar.active .beam {
        animation: radar-sweep 2s linear infinite;
      }
      .radar.active .ping {
        animation: radar-ping 2s ease-out infinite;
      }
      .radar.active .blip {
        animation: radar-blip 2s linear infinite;
      }
      @keyframes radar-sweep {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
      }
      @keyframes radar-ping {
        0% { transform: scale(0.18); opacity: 0.7; }
        70% { transform: scale(1); opacity: 0; }
        100% { transform: scale(1); opacity: 0; }
      }
      /* beam reaches 45° at 12.5% of the cycle — flash then decay */
      @keyframes radar-blip {
        0%, 9% { opacity: 0.15; }
        12.5% { opacity: 1; }
        13.5% { opacity: 1; }
        45% { opacity: 0.3; }
        100% { opacity: 0.15; }
      }
      @media (prefers-reduced-motion: reduce) {
        .radar.active .beam,
        .radar.active .ping,
        .radar.active .blip { animation: none; }
        .radar.active .ping { opacity: 0; }
      }
    `,
  ],
})
export class LoadingRadar {
  @Input() size = 18;
  @Input() active = false;
}
