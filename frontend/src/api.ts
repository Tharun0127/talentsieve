// REST calls. Live progress uses EventSource directly (see BatchPage).

export type Counts = { queued: number; processing: number; done: number; dead: number; total?: number };

export type Score = {
  score: number;
  strengths: string[];
  gaps: string[];
  verdict: string;
  candidate_name: string | null;
  provider: string;
  cached: boolean;
};

export type ResumeRow = {
  id: string;
  filename: string;
  status: "queued" | "processing" | "done" | "dead";
  attempts: number;
  error: string | null;
  result: Score | null;
};

export type BatchDetail = {
  id: string;
  title: string;
  job_description: string;
  total: number;
  created_at: string;
  counts: Partial<Counts>;
  resumes: ResumeRow[];
};

export type BatchSummary = { id: string; title: string; total: number; created_at: string };

export type DeadJob = {
  resume_id: string;
  batch_id: string;
  batch_title: string;
  filename: string;
  attempts: number;
  error: string | null;
};

const TOKEN_KEY = "talentsieve.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // storage blocked: log in again next time
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api${path}`, { ...init, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) setToken(null);
    const detail = typeof data.detail === "string" ? data.detail : "Request failed";
    throw new Error(detail);
  }
  return data as T;
}

export const api = {
  login: (email: string, password: string, signup: boolean) =>
    request<{ access_token: string }>(signup ? "/auth/signup" : "/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  batches: () => request<BatchSummary[]>("/batches"),
  batch: (id: string) => request<BatchDetail>(`/batches/${id}`),
  upload: (title: string, jobDescription: string, files: File[]) => {
    const form = new FormData();
    form.append("title", title);
    form.append("job_description", jobDescription);
    files.forEach((file) => form.append("files", file));
    return request<{ batch_id: string; total: number }>("/batches", { method: "POST", body: form });
  },
  deadJobs: () => request<DeadJob[]>("/admin/dlq"),
  retryDead: (resumeId: string) => request<{ requeued: number }>(`/admin/dlq/${resumeId}/retry`, { method: "POST" }),
  retryAllDead: () => request<{ requeued: number }>("/admin/dlq/retry-all", { method: "POST" }),
};

// EventSource cannot send headers, so the token goes in the query string.
export function progressUrl(batchId: string): string {
  return `/api/batches/${batchId}/events?token=${encodeURIComponent(getToken() ?? "")}`;
}
