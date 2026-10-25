import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
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
  readonly auth = inject(AuthService);
  readonly notice = inject(NoticeService);

  readonly username = signal('');
  readonly password = signal('');

  async signIn(): Promise<void> {
    await this.auth.login(this.username().trim(), this.password());
    this.password.set('');
  }

  async signOut(): Promise<void> {
    await this.auth.logout();
  }
}
