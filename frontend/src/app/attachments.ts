/**
 * Shared attachment handling for the composer surfaces (Home/Chat and the
 * Agent Console). Every file is read as a base64 data URL and sent to the
 * backend, which classifies and extracts it (images → gpt-5 vision,
 * PDF/DOCX/ZIP/text → inlined text). The UI only needs the data URL for image
 * thumbnails; classification here is purely for the icon vs. thumbnail choice.
 */

export interface UiAttachment {
  id: string;
  name: string;
  mime: string;
  kind: 'image' | 'file';
  size: number;
  dataUrl: string;
}

/** Wire shape POSTed to the backend (`ChatAttachment` / `MessageAttachment`). */
export interface WireAttachment {
  name: string;
  mime: string;
  data_url: string;
}

export const MAX_ATTACH_BYTES = 25 * 1024 * 1024; // 25 MB per file

function toDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

/** Read a FileList/array into UiAttachments, collecting any per-file errors. */
export async function readFiles(
  files: FileList | File[],
): Promise<{ added: UiAttachment[]; errors: string[] }> {
  const added: UiAttachment[] = [];
  const errors: string[] = [];
  for (const file of Array.from(files)) {
    if (file.size > MAX_ATTACH_BYTES) {
      errors.push(`${file.name || 'File'} is over 25 MB and was skipped.`);
      continue;
    }
    try {
      const dataUrl = await toDataUrl(file);
      const isImage = file.type.startsWith('image/') && file.type !== 'image/svg+xml';
      added.push({
        id: crypto.randomUUID(),
        name: file.name || 'file',
        mime: file.type || 'application/octet-stream',
        kind: isImage ? 'image' : 'file',
        size: file.size,
        dataUrl,
      });
    } catch {
      errors.push(`Could not read ${file.name || 'a file'}.`);
    }
  }
  return { added, errors };
}

export function toWire(atts: UiAttachment[]): WireAttachment[] {
  return atts.map((a) => ({ name: a.name, mime: a.mime, data_url: a.dataUrl }));
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Broad accept string covering images and text/code/document/zip formats. */
export const ATTACH_ACCEPT =
  'image/*,.txt,.md,.markdown,.rst,.log,.csv,.tsv,.json,.yaml,.yml,.toml,.ini,.cfg,.conf,.env,.xml,.html,.htm,.css,.scss,.js,.jsx,.ts,.tsx,.vue,.svelte,.py,.java,.kt,.c,.h,.cpp,.cc,.hpp,.cs,.go,.rs,.rb,.php,.swift,.sh,.bash,.zsh,.sql,.graphql,.proto,.svg,.pdf,.docx,.zip';
