// Compass Collab — sibling apps launched from the sidebar.
// Configure their URLs here (frontend configuration).

export interface CollabApp {
  id: 'cost' | 'pulse';
  name: string;
  url: string;
}

export const COLLAB_APPS: CollabApp[] = [
  { id: 'cost', name: 'Cost Compass', url: 'http://localhost:64989/dashboard' },
  { id: 'pulse', name: 'Pulse Compass', url: 'http://localhost:52412' },
];
