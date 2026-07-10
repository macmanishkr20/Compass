// Single source of truth for the reduced-motion preference. Every 3D effect
// checks this and degrades to a static, flat presentation when the user (or
// their OS) asks for less motion — an accessibility requirement, not a nicety.
export function prefersReducedMotion(): boolean {
  return (
    typeof matchMedia !== 'undefined' &&
    matchMedia('(prefers-reduced-motion: reduce)').matches
  );
}
