import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { NoticeService } from '../notice.service';

/**
 * The stack of short confirmations, bottom-left. Mounted once.
 *
 * Bottom-left because the other two toasts in this app (a finished turn, a
 * finished routine) sit top-right, and two kinds of message arriving in the
 * same corner read as one queue when they answer completely different
 * questions.
 */
@Component({
  selector: 'app-notice',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (svc.notices().length) {
      <div class="ntc-stack" role="status" aria-live="polite">
        @for (n of svc.notices(); track n.id) {
          <div class="ntc" [class.err]="n.tone === 'error'">
            <span class="ntc-ico" aria-hidden="true">
              @if (n.tone === 'error') {
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 7v6M12 17v.01"/><circle cx="12" cy="12" r="9" stroke-width="1.8"/></svg>
              } @else {
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
              }
            </span>
            <span class="ntc-text">{{ n.text }}</span>
            @if (n.retry && n.retryLabel) {
              <button class="ntc-act" type="button" (click)="run(n.id, n.retry!)">{{ n.retryLabel }}</button>
            }
            <button class="ntc-x" type="button" (click)="svc.dismiss(n.id)" aria-label="Dismiss">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
            </button>
          </div>
        }
      </div>
    }
  `,
  styleUrl: './notice.css',
})
export class NoticeStack {
  readonly svc = inject(NoticeService);

  /** Taking the offer dismisses the notice: leaving it up beside a second
   *  attempt would say the first failure is still true. */
  run(id: number, fn: () => void): void {
    this.svc.dismiss(id);
    fn();
  }
}
