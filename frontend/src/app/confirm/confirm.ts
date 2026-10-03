import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  effect,
  inject,
  viewChild,
} from '@angular/core';
import { ConfirmService } from '../confirm.service';

/**
 * The app's confirmation dialog. Mounted once; renders nothing until asked.
 *
 * Focus lands on Cancel rather than the destructive button, so Return at the
 * wrong moment does the harmless thing. Escape and the backdrop both decline,
 * because every other way out of a dialog here declines and a destructive one
 * is the worst place to make somebody learn a new rule.
 */
@Component({
  selector: 'app-confirm',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (svc.open(); as req) {
      <div class="cfm-scrim" (click)="svc.answer(false)"></div>
      <div class="cfm" role="alertdialog" aria-modal="true"
        aria-labelledby="cfm-title" (keydown)="onKeydown($event)">
        <h2 class="cfm-title" id="cfm-title">{{ req.title }}</h2>
        @if (req.subject) {
          <p class="cfm-subject">{{ req.subject }}</p>
        }
        @if (req.body) {
          <p class="cfm-body">{{ req.body }}</p>
        }
        <div class="cfm-foot">
          <button #cancel class="cfm-btn" type="button" (click)="svc.answer(false)">
            {{ req.cancelLabel }}
          </button>
          <button class="cfm-btn go" type="button" [class.danger]="req.danger"
            (click)="svc.answer(true)">
            {{ req.confirmLabel }}
          </button>
        </div>
      </div>
    }
  `,
  styleUrl: './confirm.css',
})
export class Confirm {
  readonly svc = inject(ConfirmService);
  private readonly cancel = viewChild<ElementRef<HTMLButtonElement>>('cancel');

  constructor() {
    effect(() => {
      if (!this.svc.open()) return;
      // After the frame the button exists in. Without focus here the keyboard
      // is still on whatever was behind the dialog, where Escape and Return
      // would reach the page rather than the question.
      queueMicrotask(() => this.cancel()?.nativeElement.focus());
    });
  }

  onKeydown(ev: KeyboardEvent): void {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      this.svc.answer(false);
    }
  }
}
