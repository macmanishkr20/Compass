/**
 * A deployment's name, written the way people say it.
 *
 * Deployment names are Azure's: lower case, hyphenated, and sometimes
 * carrying a codename that distinguishes two snapshots of the same family.
 * `gpt-6-sol` and `gpt-6-astra` are different deployments with the same
 * generation, so the codename is kept rather than folded away — dropping it
 * would put two identical entries in a picker where the choice between them
 * is the whole point.
 *
 * Derived rather than looked up in a table: a deployment added in `.env`
 * tomorrow should read sensibly without a release, and a table would have
 * printed its raw name instead.
 */
export function modelLabel(deployment: string): string {
  const raw = (deployment || '').trim();
  if (!raw) return '';
  return raw
    .split('-')
    .map((part) => {
      // gpt -> GPT. Anything already carrying a digit is a version or a
      // variant (5, 4o, 3.5) and keeps its own shape.
      if (/^gpt$/i.test(part)) return 'GPT';
      if (/\d/.test(part)) return part.toLowerCase();
      // A codename, or a qualifier like "mini" — Title Case reads as a name.
      return part.charAt(0).toUpperCase() + part.slice(1).toLowerCase();
    })
    // The generation binds to GPT with a hyphen ("GPT-5"); anything after it
    // is a separate word ("GPT-6 Sol", "GPT-4o Mini").
    .reduce((acc, part, i) => {
      if (i === 0) return part;
      if (i === 1) return `${acc}-${part}`;
      return `${acc} ${part}`;
    }, '');
}
