type ApiFetchInit = RequestInit & { timeoutMs?: number };

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  return path.startsWith("/") ? path : `/${path}`;
}

export async function apiFetch(path: string, init: ApiFetchInit = {}): Promise<Response> {
  const { timeoutMs, signal, ...rest } = init;
  const controller = new AbortController();
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null;
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  try {
    return await fetch(apiUrl(path), { ...rest, signal: controller.signal });
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function isApiReachable(): Promise<boolean> {
  try {
    const res = await apiFetch("/api/healthz", { timeoutMs: 8_000 });
    return res.ok;
  } catch {
    return false;
  }
}

export function normalizePhone(phone: string): string {
  const trimmed = phone.trim().replace(/[\s\-()]/g, "");
  if (trimmed.startsWith("+")) return trimmed;
  if (/^91\d{10}$/.test(trimmed)) return `+${trimmed}`;
  if (/^\d{10}$/.test(trimmed)) return `+91${trimmed}`;
  return trimmed;
}

export async function parseApiJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!text) return {} as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(text.slice(0, 180) || `HTTP ${res.status}`);
  }
}
