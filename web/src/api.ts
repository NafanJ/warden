import type { components } from './schema';
export type Conversation = components['schemas']['Conversation'];
export type Run = components['schemas']['Run'];
export type RunEvent = components['schemas']['RunEvent'];
export type Approval = components['schemas']['Approval'];
export type Source = components['schemas']['Source'];
export type Citation = components['schemas']['Citation'];
export type ServerSnapshot = components['schemas']['ServerSnapshot'];

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch('/api' + path, { credentials: 'same-origin', ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== '/session') window.dispatchEvent(new Event('warden-logout'));
    throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status})`);
  }
  return data;
}
export const post = <T = any>(path: string, data: unknown = {}) => api<T>(path, { method: 'POST', body: JSON.stringify(data) });
export const date = (value?: string | null) => value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'Not yet';
