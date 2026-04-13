const DEFAULT_SUPABASE_URL = import.meta.env?.VITE_SUPABASE_URL || "https://jhreeyesdtnmanolmjqu.supabase.co/rest/v1";
const DEFAULT_SUPABASE_KEY = import.meta.env?.VITE_SUPABASE_ANON_KEY || "";

export function buildSupabaseHeaders(apiKey = DEFAULT_SUPABASE_KEY) {
  return {
    apikey: apiKey,
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
    Prefer: "count=exact",
  };
}

export async function supaFetch(path, options = {}) {
  const {
    fetchImpl = fetch,
    baseUrl = DEFAULT_SUPABASE_URL,
    apiKey = DEFAULT_SUPABASE_KEY,
  } = options;

  const response = await fetchImpl(`${baseUrl}${path}`, {
    headers: buildSupabaseHeaders(apiKey),
  });

  if (!response.ok) {
    let responseText = "";
    try {
      responseText = await response.text();
    } catch {
      responseText = "";
    }

    const detail = responseText ? `: ${responseText.slice(0, 200)}` : "";
    throw new Error(`Supabase request failed (${response.status}) for ${path}${detail}`);
  }

  const countHeader = response.headers?.get?.("content-range") || "0/0";
  const countValue = Number.parseInt(countHeader.split("/")[1] || "0", 10);
  const data = await response.json();

  return {
    data,
    count: Number.isNaN(countValue) ? 0 : countValue,
  };
}

export function getJobsEmptyState(jobs) {
  if (!jobs || jobs.length === 0) {
    return "No jobs tracked yet.";
  }

  return null;
}