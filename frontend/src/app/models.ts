/**
 * The types the markdown renderer needs, and nothing else.
 *
 * Compass's own `models.ts` is 864 lines — chat messages, sessions, design
 * templates, background tasks, routine triggers — because every section in
 * that application shares it. Copying it whole would have carried a chat
 * domain model into an estimating tool, where nothing refers to it and
 * nobody maintains it. Two interfaces is what the renderer actually
 * imports, so two interfaces is what came across.
 */

export type ArtifactKind = 'html' | 'svg' | 'mermaid' | 'drawio' | 'azure';

export interface Artifact {
  id: string;
  title: string;
  kind: ArtifactKind;
  code: string;
}
