import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface AuthUser {
  username: string;
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

  readonly initials = computed(() => {
    const name = this.user()?.username ?? '';
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
        this.http.get<{ username: string }>('/v1/auth/me'),
      );
      this.user.set({ username: me.username });
    } catch {
      this.user.set(null); // no/invalid cookie — fall through to login
    } finally {
      this.checking.set(false);
    }
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
