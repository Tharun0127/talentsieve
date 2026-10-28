import { useState } from "react";
import { Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { getToken, setToken } from "./api";
import LoginPage from "./pages/LoginPage";
import UploadPage from "./pages/UploadPage";
import BatchPage from "./pages/BatchPage";
import FailedJobsPage from "./pages/FailedJobsPage";

function navClass({ isActive }: { isActive: boolean }) {
  return `rounded-md px-3 py-1.5 text-sm font-medium ${
    isActive ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-200"
  }`;
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState(Boolean(getToken()));
  const navigate = useNavigate();
  const guard = (page: JSX.Element) => (loggedIn ? page : <Navigate to="/login" replace />);

  function logout() {
    setToken(null);
    setLoggedIn(false);
    navigate("/login");
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
          <NavLink to="/" className="text-lg font-bold tracking-tight">
            Talent<span className="text-teal-600">Sieve</span>
          </NavLink>
          {loggedIn && (
            <nav className="flex items-center gap-1">
              <NavLink to="/" end className={navClass}>
                Batches
              </NavLink>
              <NavLink to="/failed" className={navClass}>
                Failed jobs
              </NavLink>
              <button onClick={logout} className="ml-2 text-sm text-slate-500 hover:text-slate-900">
                Log out
              </button>
            </nav>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Routes>
          <Route path="/login" element={<LoginPage onLogin={() => setLoggedIn(true)} />} />
          <Route path="/" element={guard(<UploadPage />)} />
          <Route path="/batches/:id" element={guard(<BatchPage />)} />
          <Route path="/failed" element={guard(<FailedJobsPage />)} />
        </Routes>
      </main>
    </div>
  );
}
