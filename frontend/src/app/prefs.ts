/**
 * Tiny cookie-backed store for non-sensitive UI preferences (theme, TTS voice),
 * so the app uses zero browser localStorage/sessionStorage. Sensitive auth lives
 * in a server-set httpOnly cookie; these are plain client prefs.
 */
export function setPref(key: string, value: string): void {
  try {
    document.cookie =
      `${key}=${encodeURIComponent(value)}; path=/; max-age=31536000; samesite=lax`;
  } catch {
    /* cookies disabled — pref simply isn't remembered */
  }
}

export function getPref(key: string): string | null {
  try {
    const m = document.cookie.match(new RegExp('(?:^|; )' + key + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  } catch {
    return null;
  }
}
