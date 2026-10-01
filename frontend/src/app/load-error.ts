import {
  ChangeDetectionStrategy,
  Component,
  booleanAttribute,
  input,
  output,
} from '@angular/core';

/**
 * "Couldn't load this" — the one way Compass says a fetch did not arrive.
 *
 * There is exactly one of these because there was nearly none. A request that
 * failed used to leave whatever it was filling empty, which is the same thing
 * an empty conversation, an empty sidebar and a design with nothing in it
 * look like — so the one state that resolves itself by pressing a button was
 * indistinguishable from the three that do not.
 *
 * It is deliberately not a modal. A dialog over the whole window would say
 * that everything is broken, when what failed is one request: the sidebar is
 * still usable when a conversation does not open, and the conversation is
 * still readable when the sidebar list does not. So this renders in the space
 * the missing thing would have filled, and the rest of the application keeps
 * working around it.
 *
 * Three things, in this order, because that is the order they are wanted in:
 *   what happened, in words — and that nothing is lost, which is the first
 *   thing anyone wants to know;
 *   the detail, for whoever is going to report it;
 *   the way out — retrying, and the thing to do instead.
 *
 * `compact` is for somewhere narrow, like a sidebar, where the full card
 * would be wider than the column it is in.
 */
@Component({
  selector: 'app-load-error',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="lderr" [class.compact]="compact()" role="alert">
      <div class="ic" aria-hidden="true">
        <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 8v5M12 17h.01" /><circle cx="12" cy="12" r="9" />
        </svg>
      </div>
      <b>{{ heading() }}</b>
      <p>{{ body() }}</p>
      @if (detail()) {
        <span class="code">{{ detail() }}</span>
      }
      <div class="acts2">
        <button class="btn2 pri" type="button" (click)="retry.emit()">
          <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6" />
          </svg>
          {{ retryLabel() }}
        </button>
        @if (secondaryLabel()) {
          <button class="btn2" type="button" (click)="secondary.emit()">
            {{ secondaryLabel() }}
          </button>
        }
      </div>
    </div>
  `,
})
export class LoadError {
  readonly heading = input('Couldn’t load this');
  /** The sentence under the heading. Says what failed and that nothing is
   *  lost, because "is my work gone?" is the question being asked. */
  readonly body = input('Nothing is lost — it is still there.');
  /** One line for whoever is going to report it. Empty hides the row. */
  readonly detail = input('');
  readonly retryLabel = input('Try again');
  /** Empty hides the second button, for the places with nothing else to do. */
  readonly secondaryLabel = input('');
  /** Transformed, so `compact` works as a bare attribute rather than only as
   *  `[compact]="true"` — which is how it reads at the call site. */
  readonly compact = input(false, { transform: booleanAttribute });

  readonly retry = output<void>();
  readonly secondary = output<void>();
}
