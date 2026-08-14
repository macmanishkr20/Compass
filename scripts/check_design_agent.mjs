#!/usr/bin/env node
/**
 * Syntax-check the canvas editor agent.
 *
 * The agent is a JavaScript program that lives inside a TypeScript template
 * literal, so `tsc` only sees a string: a stray bracket in it compiles clean
 * and then fails silently at runtime, inside a sandboxed frame, where nothing
 * surfaces the error. This parses it the way a browser would.
 *
 *     node scripts/check_design_agent.mjs
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, '..', 'frontend', 'src', 'app', 'design', 'design-editor.ts'),
  'utf8',
);

const match = source.match(/export const EDITOR_SCRIPT = String\.raw`([\s\S]*?)\n`;/);
if (!match) {
  console.error('check_design_agent: could not find EDITOR_SCRIPT in design-editor.ts');
  process.exit(2);
}

const script = match[1];

// A literal </script> anywhere in the source would close the tag early when the
// agent is injected into a page, truncating it.
if (script.includes('</script>')) {
  console.error('check_design_agent: the agent contains a literal </script>');
  process.exit(1);
}

try {
  new vm.Script(script, { filename: 'design-editor-agent.js' });
} catch (err) {
  console.error(`check_design_agent: ${err.name}: ${err.message}`);
  process.exit(1);
}

console.log(`check_design_agent: OK (${script.length} chars)`);
