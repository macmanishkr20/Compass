import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth.service';

/** Auth rides in a secure httpOnly cookie (no token in browser storage), so we
 * just send credentials on same-origin API calls and funnel 401s back into the
 * auth state so the shell drops to the login screen. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const authed = req.url.startsWith('/v1')
    ? req.clone({ withCredentials: true })
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
