type FetchOptions = RequestInit & {
  query?: Record<string, string | number | boolean | undefined>;
};

function buildApiUrl(path: string, query?: FetchOptions["query"]) {
  const normalized = path.startsWith("/api/") ? path : `/api${path}`;
  const params = new URLSearchParams();
  Object.entries(query || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      params.set(key, String(value));
    }
  });
  const qs = params.toString();
  return qs ? `${normalized}?${qs}` : normalized;
}

export async function apiFetch<T>(path: string, options: FetchOptions = {}): Promise<T> {
  const response = await fetch(buildApiUrl(path, options.query), {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${body}`);
  }

  return response.json() as Promise<T>;
}
