import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth.service';

/** Attaches the bearer token to every API call and funnels 401s back into
 * the auth state so the shell drops to the login screen. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const token = auth.token;
  const authed =
    token && req.url.startsWith('/v1') && !req.url.startsWith('/v1/auth/login')
      ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } })
      : req;
  return next(authed).pipe(
    catchError((err: HttpErrorResponse) => {
      if (err.status === 401 && !req.url.includes('/v1/auth/login')) {
        auth.sessionExpired();
      }
      return throwError(() => err);
    }),
  );
};
