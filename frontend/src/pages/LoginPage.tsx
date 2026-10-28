import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setToken } from "../api";

export default function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [signup, setSignup] = useState(false);
  const [email, setEmail] = useState("recruiter@talentsieve.dev");
  const [password, setPassword] = useState("recruit123");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      setToken((await api.login(email, password, signup)).access_token);
      onLogin();
      navigate("/");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form onSubmit={submit} className="mx-auto mt-10 max-w-sm space-y-3 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h1 className="text-xl font-bold">{signup ? "Create a recruiter account" : "Recruiter login"}</h1>
      <input className="w-full rounded-md border border-slate-300 px-3 py-2" type="email" value={email}
        onChange={(e) => setEmail(e.target.value)} placeholder="Email" required />
      <input className="w-full rounded-md border border-slate-300 px-3 py-2" type="password" value={password}
        onChange={(e) => setPassword(e.target.value)} placeholder="Password (min 8)" minLength={8} required />
      {error && <p className="text-sm text-rose-600">{error}</p>}
      <button className="w-full rounded-md bg-teal-600 py-2 font-semibold text-white hover:bg-teal-700">
        {signup ? "Sign up" : "Log in"}
      </button>
      <button type="button" onClick={() => setSignup(!signup)} className="text-sm text-teal-700 hover:underline">
        {signup ? "Have an account? Log in" : "No account? Sign up"}
      </button>
    </form>
  );
}
