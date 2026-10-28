import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type BatchSummary } from "../api";

const MAX_FILES = 50;

export default function UploadPage() {
  const [title, setTitle] = useState("");
  const [jobDescription, setJobDescription] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [batches, setBatches] = useState<BatchSummary[]>([]);
  const navigate = useNavigate();

  useEffect(() => {
    api.batches().then(setBatches).catch(() => setBatches([]));
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!files.length) return setError("Choose at least one PDF.");
    if (files.length > MAX_FILES) return setError(`At most ${MAX_FILES} resumes per batch.`);
    setBusy(true);
    setError(null);
    try {
      // Returns immediately with a batch id; scoring happens in the workers.
      const { batch_id } = await api.upload(title, jobDescription, files);
      navigate(`/batches/${batch_id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
      <form onSubmit={submit} className="space-y-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h1 className="text-xl font-bold">Screen a batch of resumes</h1>
        <input className="w-full rounded-md border border-slate-300 px-3 py-2" placeholder="Role title, e.g. Senior Backend Engineer"
          value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} />
        <textarea className="h-48 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          placeholder="Paste the job description..." value={jobDescription}
          onChange={(e) => setJobDescription(e.target.value)} required minLength={20} />
        <label className="block rounded-lg border-2 border-dashed border-slate-300 p-6 text-center hover:border-teal-400">
          <input type="file" accept="application/pdf,.pdf" multiple className="hidden"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
          <p className="font-medium">{files.length ? `${files.length} PDF(s) selected` : "Click to choose resumes (PDF)"}</p>
          <p className="text-xs text-slate-500">Up to {MAX_FILES} files, 5 MB each</p>
        </label>
        {error && <p className="text-sm text-rose-600">{error}</p>}
        <button disabled={busy} className="rounded-md bg-teal-600 px-5 py-2 font-semibold text-white hover:bg-teal-700 disabled:opacity-50">
          {busy ? "Uploading..." : "Upload and score"}
        </button>
      </form>

      <aside className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <h2 className="mb-2 font-semibold">Previous batches</h2>
        {batches.length === 0 && <p className="text-sm text-slate-500">None yet.</p>}
        <ul className="space-y-1">
          {batches.map((b) => (
            <li key={b.id}>
              <Link to={`/batches/${b.id}`} className="flex justify-between rounded px-2 py-1 text-sm hover:bg-slate-100">
                <span className="truncate">{b.title}</span>
                <span className="text-slate-500">{b.total}</span>
              </Link>
            </li>
          ))}
        </ul>
      </aside>
    </div>
  );
}
