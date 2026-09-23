import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface AuthUser {
  username: string;
  /** What this person asked to be called. Empty until they say. */
  displayName?: string;
}

/**
 * Session auth state. The token lives ONLY in a secure httpOnly cookie set by
 * the server — never in browser storage — so JS can't read it and the client
 * just relies on the cookie riding along (withCredentials). `user` is the
 * reactive source of truth the shell gates on: null = show the login screen.
 * When the backend reports auth disabled, we run as "guest" with no login.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);

  readonly user = signal<AuthUser | null>(null);
  readonly checking = signal(true);
  readonly authEnabled = signal(true);
  readonly loginError = signal<string | null>(null);
  readonly busy = signal(false);

  /** What to call this person, or '' when nobody has said.
   *
   *  A name someone has given (Customize → "What Compass calls you") wins
   *  over anything derived from the login, because it is the only one of the
   *  two that is actually their name.
   *
   *  Failing that, a login is used only when it reads like a name. Stripping
   *  the domain off an address was not enough: `macmanishkr20@gmail.com`
   *  became "Evening, macmanishkr20", which is not a name, is an account
   *  handle on screen for whoever is standing behind them, and is worse than
   *  no greeting at all. So a handle carrying digits, or one that is a single
   *  opaque run of letters, is treated as not-a-name and the greeting simply
   *  leaves it out. `first.last` and `alice` still come through, because
   *  those are names people chose to be known by.
   */
  readonly displayName = computed(() => {
    const given = (this.user()?.displayName ?? '').trim();
    if (given) return given;

    const local = (this.user()?.username ?? '').trim().split('@')[0];
    if (!local || /\d/.test(local)) return '';
    const words = local.split(/[._-]+/).filter(Boolean);
    if (words.length < 2) {
      // A single word is a name only if it looks like one — short enough to
      // be read aloud, and not an obvious identifier.
      return local.length <= 12
        ? local.charAt(0).toUpperCase() + local.slice(1)
        : '';
    }
    return words
      .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ');
  });

  readonly initials = computed(() => {
    // Falls back to the login where the greeting does not: two letters in an
    // avatar is a monogram, not an address on display, and an empty circle
    // tells nobody which account they are in.
    const name = this.displayName() || (this.user()?.username ?? '').split('@')[0];
    const words = name.split(/[\s._-]+/).filter(Boolean);
    if (words.length >= 2) {
      return (words[0][0] + words[1][0]).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase() || '??';
  });

  /** Boot check: ask the server who we are (the cookie rides along), or bypass
   *  when auth is disabled. A 401 just means "not signed in". */
  async restore(authEnabled: boolean): Promise<void> {
    this.authEnabled.set(authEnabled);
    try {
      if (!authEnabled) {
        this.user.set({ username: 'guest' });
        return;
      }
      const me = await firstValueFrom(
        this.http.get<{ username: string; display_name?: string }>('/v1/auth/me'),
      );
      this.user.set({ username: me.username, displayName: me.display_name ?? '' });
    } catch {
      this.user.set(null); // no/invalid cookie — fall through to login
    } finally {
      this.checking.set(false);
    }
  }

  /** Tell the server what to call this person. Empty clears it and the
   *  greeting goes back to having no name in it. */
  async setDisplayName(name: string): Promise<void> {
    const res = await firstValueFrom(
      this.http.post<{ display_name: string }>('/v1/auth/me/name', { name }),
    );
    const current = this.user();
    if (current) this.user.set({ ...current, displayName: res.display_name });
  }

  async login(username: string, password: string): Promise<boolean> {
    this.busy.set(true);
    this.loginError.set(null);
    try {
      // The server sets the httpOnly cookie on this response; we keep nothing.
      const res = await firstValueFrom(
        this.http.post<{ user: AuthUser }>('/v1/auth/login', { username, password }),
      );
      this.user.set(res.user);
      return true;
    } catch (err: unknown) {
      const status = (err as { status?: number }).status;
      this.loginError.set(
        status === 401
          ? 'Invalid username or password.'
          : 'Could not reach the server. Is the backend running?',
      );
      return false;
    } finally {
      this.busy.set(false);
    }
  }

  async logout(): Promise<void> {
    try {
      await firstValueFrom(this.http.post('/v1/auth/logout', {}));
    } catch {
      /* clearing the cookie is best-effort */
    }
    this.user.set(null);
  }

  /** Called by the interceptor on a 401 from any API call. */
  sessionExpired(): void {
    if (this.authEnabled() && this.user()) {
      this.user.set(null);
      this.loginError.set('Your session expired — please sign in again.');
    }
  }
}
