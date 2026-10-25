import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { FormsModule } from '@angular/forms';

import { Estimate } from './estimate/estimate';
import { AuthService } from './auth.service';
import { NoticeService } from './notice.service';

/**
 * The whole shell.
 *
 * In Compass this file carries seven tabs, a sidebar, a theme switcher and
 * an artifact pane. Here the application IS the estimate screen, so the
 * shell's only remaining jobs are getting somebody signed in and showing
 * what the notice service has to say.
 *
 * The sign-in state is the AuthService's and is read from it rather than
 * mirrored: `busy`, `checking` and `loginError` are already signals over
 * there, and a second copy of them here is a second answer to "did that
 * work" that can disagree with the first.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [Estimate, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  private readonly http = inject(HttpClient);
  readonly auth = inject(AuthService);
  readonly notice = inject(NoticeService);

  readonly username = signal('');
  readonly password = signal('');
  readonly unreachable = signal('');

  constructor() {
    void this.start();
  }

  /** Ask the server whether a sign-in is needed, then restore the session.
   *
   *  `checking` starts true so the app renders nothing until this answers —
   *  without it the login flashes up for anybody who already has a cookie.
   *  It is cleared in `finally` whatever happened, because a backend that
   *  is down should produce a message rather than a spinner that never
   *  stops. */
  private async start(): Promise<void> {
    try {
      const health = await firstValueFrom(
        this.http.get<{ auth?: boolean }>('/healthz'),
      );
      await this.auth.restore(health.auth ?? true);
    } catch {
      this.unreachable.set(
        'Backend unreachable. Start it with: ' +
          'PYTHONPATH=. uvicorn estimator.api.server:app --port 8000',
      );
    } finally {
      this.auth.checking.set(false);
    }
  }

  async signIn(): Promise<void> {
    await this.auth.login(this.username().trim(), this.password());
    this.password.set('');
  }

  async signOut(): Promise<void> {
    await this.auth.logout();
  }
}
