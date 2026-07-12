// A compact, self-contained Azure service icon set. Each icon is a rounded
// tile in the service's brand-family colour with a white line glyph — clean,
// consistent, and (crucially) embeddable as a data: URI so it renders in the
// inline preview, in draw.io / diagrams.net, and in exported PNG/SVG/Visio
// without depending on any external stencil library. The vocabulary keys are
// what the model references in its diagram spec.

export interface AzureIcon {
  /** Friendly default label if the spec doesn't give one. */
  name: string;
  /** Tile background colour (brand family). */
  color: string;
  /** White line-glyph markup, authored in a 0..24 box, centred in the tile. */
  glyph: string;
}

// Brand-family colours.
const NET = '#0F6CBD'; // networking / edge
const WEB = '#3B7DD8'; // compute / web
const INT = '#7A4EA8'; // integration
const IDN = '#2C6FBF'; // identity
const SEC = '#3B873E'; // security
const DAT = '#0072C6'; // data
const MON = '#68217A'; // monitoring / management
const CACHE = '#C4472E'; // redis
const CLIENT = '#3999E6'; // users / client
const GEN = '#5A6B7B'; // generic

export const AZURE_ICONS: Record<string, AzureIcon> = {
  users: {
    name: 'Users',
    color: CLIENT,
    glyph: '<circle cx="12" cy="8" r="4"/><path d="M4 22c0-4.4 3.6-7 8-7s8 2.6 8 7"/>',
  },
  front_door: {
    name: 'Azure Front Door',
    color: NET,
    glyph: '<path d="M12 2l8 3.5v5.5c0 5.3-3.4 8.4-8 9.8-4.6-1.4-8-4.5-8-9.8V5.5z"/><path d="M12 7v6M9 10h6"/>',
  },
  application_gateway: {
    name: 'Application Gateway',
    color: NET,
    glyph: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 12h8M13 9l3 3-3 3"/>',
  },
  load_balancer: {
    name: 'Load Balancer',
    color: NET,
    glyph: '<circle cx="12" cy="4" r="2.4"/><circle cx="4" cy="20" r="2.4"/><circle cx="12" cy="20" r="2.4"/><circle cx="20" cy="20" r="2.4"/><path d="M12 6.4v6M12 12h-8v5.6M12 12v5.6M12 12h8v5.6"/>',
  },
  app_service: {
    name: 'App Service',
    color: WEB,
    glyph: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
  },
  function_app: {
    name: 'Function App',
    color: WEB,
    glyph: '<path d="M13 2L4 14h6l-1 8 10-13h-6z" fill="#fff" stroke="none"/>',
  },
  aks: {
    name: 'Kubernetes Service',
    color: WEB,
    glyph: '<path d="M12 2l8.5 4v8L12 22 3.5 14V6z"/><path d="M12 7v10M8 9.5l8 5M16 9.5l-8 5"/>',
  },
  vm: {
    name: 'Virtual Machine',
    color: WEB,
    glyph: '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M9 20h6M12 16v4"/>',
  },
  apim: {
    name: 'API Management',
    color: INT,
    glyph: '<path d="M9 3C6.5 3 8 8 4.5 8 8 8 6.5 13 9 13M15 3c2.5 0 1 5 4.5 5-3.5 0-2 5-4.5 5"/><circle cx="12" cy="18" r="2"/>',
  },
  service_bus: {
    name: 'Service Bus',
    color: INT,
    glyph: '<rect x="2" y="9" width="20" height="6" rx="1.5"/><path d="M6 12h2M11 12h2M16 12h2"/>',
  },
  event_hub: {
    name: 'Event Hubs',
    color: INT,
    glyph: '<ellipse cx="12" cy="12" rx="9" ry="5"/><path d="M12 7v10M8 9v6M16 9v6"/>',
  },
  entra_id: {
    name: 'Microsoft Entra ID',
    color: IDN,
    glyph: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2.4"/><path d="M5.5 16.5c.4-2 1.8-3 3.5-3s3.1 1 3.5 3M14.5 9h3.5M14.5 12h3.5M14.5 15h2"/>',
  },
  key_vault: {
    name: 'Key Vault',
    color: SEC,
    glyph: '<circle cx="8" cy="8" r="4"/><path d="M11 11l9 9M17 17l2-2M15 19l1.5-1.5"/>',
  },
  sql_database: {
    name: 'SQL Database',
    color: DAT,
    glyph: '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v14c0 1.7 3.1 3 7 3s7-1.3 7-3V5"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  },
  cosmos_db: {
    name: 'Cosmos DB',
    color: DAT,
    glyph: '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="9" ry="3.6"/><ellipse cx="12" cy="12" rx="3.6" ry="9"/>',
  },
  redis: {
    name: 'Cache for Redis',
    color: CACHE,
    glyph: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
  },
  storage: {
    name: 'Storage Account',
    color: DAT,
    glyph: '<path d="M3 8l9-5 9 5-9 5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  },
  app_insights: {
    name: 'Application Insights',
    color: MON,
    glyph: '<path d="M2 13h4l3-9 4 17 3-8h6"/>',
  },
  log_analytics: {
    name: 'Log Analytics',
    color: MON,
    glyph: '<circle cx="10" cy="10" r="6"/><path d="M14.5 14.5L21 21M7 9h6M7 11.5h4"/>',
  },
  monitor: {
    name: 'Azure Monitor',
    color: MON,
    glyph: '<rect x="2" y="4" width="20" height="13" rx="1.5"/><path d="M6 12l3-4 3 5 2-3 4 2M9 21h6M12 17v4"/>',
  },
  vnet: {
    name: 'Virtual Network',
    color: '#0078D4',
    glyph: '<circle cx="12" cy="4" r="2.5"/><circle cx="4" cy="18" r="2.5"/><circle cx="20" cy="18" r="2.5"/><path d="M12 6.5v4M11 12l-5 4M13 12l5 4"/>',
  },
  generic: {
    name: 'Azure Service',
    color: GEN,
    glyph: '<path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M12 12l9-5M12 12v10M12 12L3 7"/>',
  },
};

/** A few aliases so common phrasings from the model still resolve. */
const ALIASES: Record<string, string> = {
  frontdoor: 'front_door',
  afd: 'front_door',
  appgw: 'application_gateway',
  gateway: 'application_gateway',
  lb: 'load_balancer',
  webapp: 'app_service',
  app: 'app_service',
  functions: 'function_app',
  function: 'function_app',
  kubernetes: 'aks',
  k8s: 'aks',
  virtual_machine: 'vm',
  api_management: 'apim',
  servicebus: 'service_bus',
  eventhub: 'event_hub',
  eventhubs: 'event_hub',
  entra: 'entra_id',
  aad: 'entra_id',
  azure_ad: 'entra_id',
  keyvault: 'key_vault',
  sql: 'sql_database',
  sqldb: 'sql_database',
  database: 'sql_database',
  cosmos: 'cosmos_db',
  cache: 'redis',
  blob: 'storage',
  storage_account: 'storage',
  insights: 'app_insights',
  appinsights: 'app_insights',
  loganalytics: 'log_analytics',
  network: 'vnet',
  client: 'users',
  user: 'users',
  browser: 'users',
};

export function resolveIcon(service: string): AzureIcon {
  const key = (service || '').toLowerCase().replace(/[\s-]+/g, '_');
  return AZURE_ICONS[key] ?? AZURE_ICONS[ALIASES[key]] ?? AZURE_ICONS['generic'];
}

/** A standalone 48×48 SVG string for the service tile. */
export function iconSvg(service: string): string {
  const ic = resolveIcon(service);
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">` +
    `<rect x="1" y="1" width="46" height="46" rx="10" fill="${ic.color}"/>` +
    `<g transform="translate(12,12)" fill="none" stroke="#ffffff" stroke-width="1.9" ` +
    `stroke-linecap="round" stroke-linejoin="round">${ic.glyph}</g></svg>`
  );
}

/** The tile as a base64 data: URI, for draw.io image shapes. */
export function iconDataUri(service: string): string {
  // Icons are ASCII, so btoa is safe.
  return 'data:image/svg+xml;base64,' + btoa(iconSvg(service));
}
