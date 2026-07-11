import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/**
 * Claude-style starburst loading icon: a 12-ray coral asterisk that slowly
 * rotates and breathes while `active`, and freezes the moment the response
 * finishes or is stopped. The animation is driven purely by the [active]
 * binding, so state and motion can never disagree.
 *
 * Global prefers-reduced-motion rule in styles.css disables the animation.
 */
@Component({
  selector: 'app-loading-star',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <svg
      class="star"
      [class.active]="active"
      [attr.width]="size"
      [attr.height]="size"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <g stroke="var(--accent)" stroke-width="2.4" stroke-linecap="round">
        <!-- 6 diameters -> 12 rays -->
        <line x1="12" y1="2.5" x2="12" y2="21.5" />
        <line x1="2.5" y1="12" x2="21.5" y2="12" />
        <line x1="5.3" y1="5.3" x2="18.7" y2="18.7" />
        <line x1="18.7" y1="5.3" x2="5.3" y2="18.7" />
        <line x1="3.8" y1="8.1" x2="20.2" y2="15.9" />
        <line x1="20.2" y1="8.1" x2="3.8" y2="15.9" />
      </g>
    </svg>
  `,
  styles: [
    `
      :host { display: inline-grid; place-items: center; line-height: 0; }
      .star { display: block; transform-origin: center; }
      .star.active {
        animation:
          star-spin 3.2s linear infinite,
          star-breathe 1.4s ease-in-out infinite;
      }
      @keyframes star-spin {
        to { rotate: 360deg; }
      }
      @keyframes star-breathe {
        0%, 100% { scale: 1; opacity: 1; }
        50% { scale: 0.82; opacity: 0.72; }
      }
      @media (prefers-reduced-motion: reduce) {
        .star.active { animation: none; }
      }
    `,
  ],
})
export class LoadingStar {
  @Input() size = 18;
  @Input() active = false;
}
