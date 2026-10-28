import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type DeadJob } from "../api";

// The dead-letter queue: jobs that failed permanently or ran out of retries.
export default function FailedJobsPage() {
  const [jobs, setJobs] = useState<DeadJob[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => api.deadJobs().then(setJobs), []);
  useEffect(() => {
    void load();
  }, [load]);

  async function retry(resumeId: string) {
    await api.retryDead(resumeId);
    setMessage("Job sent back to the queue.");
    void load();
  }

  async function retryAll() {
    const { requeued } = await api.retryAllDead();
    setMessage(`${requeued} job(s) sent back to the queue.`);
    void load();
  }

  if (!jobs) return <p className="text-slate-500">Loading...</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Failed jobs</h1>
          <p className="text-sm text-slate-500">
            The dead-letter queue: resumes that failed permanently (e.g. a corrupt PDF) or used all 5 attempts.
          </p>
        </div>
        {jobs.length > 0 && (
          <button onClick={retryAll} className="rounded-md bg-rose-600 px-4 py-2 text-sm font-semibold text-white hover:bg-rose-700">
            Retry all ({jobs.length})
          </button>
        )}
      </div>
      {message && <p className="rounded bg-teal-50 p-2 text-sm text-teal-800">{message}</p>}
      {jobs.length === 0 ? (
        <p className="rounded-xl border border-slate-200 bg-white p-6 text-center text-slate-500">No failed jobs. 🎉</p>
      ) : (
        <ul className="space-y-2">
          {jobs.map((job) => (
            <li key={job.resume_id} className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-rose-200 bg-white p-4 shadow-sm">
              <div className="min-w-0">
                <p className="font-medium">{job.filename}</p>
                <p className="text-xs text-slate-500">
                  <Link to={`/batches/${job.batch_id}`} className="hover:underline">{job.batch_title}</Link> · attempts {job.attempts}
                </p>
                <p className="mt-1 break-words font-mono text-xs text-rose-700">{job.error}</p>
              </div>
              <button onClick={() => retry(job.resume_id)}
                className="rounded border border-rose-300 px-3 py-1 text-sm text-rose-700 hover:bg-rose-50">Retry</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
