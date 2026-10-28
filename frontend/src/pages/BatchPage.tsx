import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, progressUrl, type BatchDetail, type Counts, type ResumeRow } from "../api";

const SEGMENTS: { key: keyof Counts; label: string; bar: string; text: string }[] = [
  { key: "done", label: "Done", bar: "bg-emerald-500", text: "text-emerald-700" },
  { key: "processing", label: "Processing", bar: "bg-sky-500", text: "text-sky-700" },
  { key: "queued", label: "Queued", bar: "bg-slate-300", text: "text-slate-600" },
  { key: "dead", label: "Failed", bar: "bg-rose-500", text: "text-rose-700" },
];

function scoreClass(score: number) {
  if (score >= 70) return "bg-emerald-100 text-emerald-800";
  if (score >= 50) return "bg-amber-100 text-amber-800";
  return "bg-rose-100 text-rose-800";
}

export default function BatchPage() {
  const id = useParams().id!;
  const [batch, setBatch] = useState<BatchDetail | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => api.batch(id).then(setBatch).catch((e) => setError(e.message)), [id]);

  useEffect(() => {
    void load();
    // Live progress over Server-Sent Events. EventSource reconnects by itself.
    const source = new EventSource(progressUrl(id));
    source.onmessage = (event) => {
      setCounts(JSON.parse(event.data));
      void load(); // refresh the table as results land
    };
    source.addEventListener("complete", (event) => {
      setCounts(JSON.parse((event as MessageEvent).data));
      void load();
      source.close();
    });
    return () => source.close();
  }, [id, load]);

  async function retry(row: ResumeRow) {
    await api.retryDead(row.id);
    void load();
  }

  if (error) return <p className="text-rose-600">{error}</p>;
  if (!batch) return <p className="text-slate-500">Loading...</p>;

  const total = batch.total;
  const live: Counts = counts ?? {
    queued: batch.counts.queued ?? 0, processing: batch.counts.processing ?? 0,
    done: batch.counts.done ?? 0, dead: batch.counts.dead ?? 0,
  };
  const finished = live.done + live.dead;

  return (
    <div className="space-y-5">
      <div>
        <Link to="/" className="text-sm text-teal-700 hover:underline">← Batches</Link>
        <h1 className="mt-1 text-2xl font-bold">{batch.title}</h1>
        <details className="text-sm text-slate-600">
          <summary className="cursor-pointer">Job description</summary>
          <p className="mt-2 whitespace-pre-wrap">{batch.job_description}</p>
        </details>
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-2 flex justify-between text-sm">
          <span className="font-semibold">{finished === total ? "Finished" : "Scoring in the background..."}</span>
          <span className="text-slate-500">{finished} / {total}</span>
        </div>
        <div className="flex h-3 overflow-hidden rounded-full bg-slate-100">
          {SEGMENTS.map((s) => (
            <div key={s.key} className={`${s.bar} transition-all duration-500`}
              style={{ width: `${((live[s.key] ?? 0) / total) * 100}%` }} />
          ))}
        </div>
        <div className="mt-2 flex flex-wrap gap-4 text-sm">
          {SEGMENTS.map((s) => (
            <span key={s.key} className={s.text}>{s.label}: <b>{live[s.key] ?? 0}</b></span>
          ))}
        </div>
      </section>

      <section className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-100 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">#</th>
              <th className="px-3 py-2">Candidate</th>
              <th className="px-3 py-2">Score</th>
              <th className="px-3 py-2">Verdict</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {batch.resumes.map((row, index) => (
              <Fragment key={row.id}>
                <tr className="cursor-pointer border-t border-slate-100 hover:bg-slate-50"
                  onClick={() => setExpanded(expanded === row.id ? null : row.id)}>
                  <td className="px-3 py-2 text-slate-400">{row.result ? index + 1 : "–"}</td>
                  <td className="px-3 py-2">
                    <p className="font-medium">{row.result?.candidate_name ?? row.filename}</p>
                    <p className="text-xs text-slate-400">{row.filename}</p>
                  </td>
                  <td className="px-3 py-2">
                    {row.result ? (
                      <span className={`rounded px-2 py-0.5 font-bold ${scoreClass(row.result.score)}`}>{row.result.score}</span>
                    ) : "–"}
                  </td>
                  <td className="px-3 py-2 text-slate-600">{row.result?.verdict ?? row.error ?? ""}</td>
                  <td className="px-3 py-2">
                    <StatusPill row={row} onRetry={() => retry(row)} />
                  </td>
                </tr>
                {expanded === row.id && row.result && (
                  <tr className="bg-slate-50">
                    <td />
                    <td colSpan={4} className="px-3 py-3">
                      <div className="grid gap-4 md:grid-cols-2">
                        <List title="Strengths" items={row.result.strengths} color="text-emerald-700" />
                        <List title="Gaps" items={row.result.gaps} color="text-rose-700" />
                      </div>
                      <p className="mt-2 text-xs text-slate-400">
                        scored by {row.result.provider}{row.result.cached ? " (from cache, no LLM call)" : ""} · attempts {row.attempts}
                      </p>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function StatusPill({ row, onRetry }: { row: ResumeRow; onRetry: () => void }) {
  if (row.status === "dead") {
    return (
      <span className="flex items-center gap-2">
        <span className="rounded bg-rose-100 px-2 py-0.5 text-xs text-rose-800">failed</span>
        <button onClick={(e) => { e.stopPropagation(); onRetry(); }}
          className="rounded border border-rose-300 px-2 py-0.5 text-xs text-rose-700 hover:bg-rose-50">Retry</button>
      </span>
    );
  }
  const style = { done: "bg-emerald-100 text-emerald-800", processing: "bg-sky-100 text-sky-800", queued: "bg-slate-100 text-slate-600" }[row.status];
  const label = row.status === "queued" && row.attempts > 0 ? `retrying (attempt ${row.attempts + 1})` : row.status;
  return <span className={`rounded px-2 py-0.5 text-xs ${style}`} title={row.error ?? ""}>{label}</span>;
}

function List({ title, items, color }: { title: string; items: string[]; color: string }) {
  return (
    <div>
      <p className={`text-xs font-semibold uppercase ${color}`}>{title}</p>
      <ul className="mt-1 list-disc pl-5">
        {items.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </div>
  );
}
