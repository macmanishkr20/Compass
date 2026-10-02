import { Injectable, signal } from '@angular/core';

/**
 * "Edit this one", carried from a picture in the transcript to the composer
 * that will send the instruction.
 *
 * A signal rather than a direct call because of who the two parties are: the
 * button is inside the markdown renderer, which is several levels down and
 * is used by both Home and Code, and the composer belongs to whichever of
 * those is on screen. Wiring them to each other would mean the renderer
 * knowing which module it is in.
 *
 * The request is the picture's URL and nothing else. What to change is the
 * person's to say — they type it into the composer as they would any other
 * message, and the model edits from there.
 */
@Injectable({ providedIn: 'root' })
export class ImageActionsService {
  /** The picture somebody pressed Edit on, or "" once it has been taken up. */
  readonly editing = signal('');

  requestEdit(url: string): void {
    this.editing.set(url);
  }

  /** Called by whichever composer picked it up. */
  taken(): void {
    this.editing.set('');
  }
}
