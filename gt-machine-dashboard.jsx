import { useState, useEffect, useCallback } from "react";
import { Search, Users, Briefcase, AlertCircle, ChevronDown, ChevronUp, ExternalLink, RefreshCw, Filter, X } from "lucide-react";
import { getJobsEmptyState, supaFetch } from "./dashboard-data.mjs";

// ─── CONFIG ──────────────────────────────────────────
// IMPORTANT: Use the anon key here (not service_role). Set via environment variable at build time.
// The anon key is safe for client-side use when combined with Row Level Security (RLS) policies.

// ─── MAIN APP ────────────────────────────────────────
export default function Dashboard() {
  const [tab, setTab] = useState("overview");
  const [stats, setStats] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [people, setPeople] = useState([]);
  const [peopleCount, setPeopleCount] = useState(0);
  const [recentPeople, setRecentPeople] = useState([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortCol, setSortCol] = useState("created_at");
  const [sortDir, setSortDir] = useState("desc");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const PAGE_SIZE = 25;

  const loadStats = useCallback(async () => {
    const [total, queued, enriched, drafted, sent, archived, jobsRes, recentRes] = await Promise.all([
      supaFetch("/people?select=id"),
      supaFetch("/people?select=id&status=eq.queued"),
      supaFetch("/people?select=id&status=eq.enriched"),
      supaFetch("/people?select=id&status=eq.drafted"),
      supaFetch("/people?select=id&status=eq.sent"),
      supaFetch("/people?select=id&status=eq.archived"),
      supaFetch("/jobs?select=*&order=created_at.desc&limit=20"),
      supaFetch("/people?select=*&order=created_at.desc&limit=10"),
    ]);
    setStats({
      total: total.count,
      queued: queued.count,
      enriched: enriched.count,
      drafted: drafted.count,
      sent: sent.count,
      archived: archived.count,
    });
    setJobs(jobsRes.data || []);
    setRecentPeople(recentRes.data || []);
  }, []);

  const loadPeople = useCallback(async () => {
    let path = `/people?select=*&order=${sortCol}.${sortDir}&limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`;
    if (statusFilter !== "all") path += `&status=eq.${statusFilter}`;
    if (search) path += `&or=(name.ilike.*${encodeURIComponent(search)}*,company_name.ilike.*${encodeURIComponent(search)}*,email.ilike.*${encodeURIComponent(search)}*,title.ilike.*${encodeURIComponent(search)}*)`;
    const res = await supaFetch(path);
    setPeople(res.data || []);
    setPeopleCount(res.count);
  }, [sortCol, sortDir, page, statusFilter, search]);

  useEffect(() => {
    let active = true;

    loadStats()
      .then(() => {
        if (!active) {
          return;
        }
        setError("");
        setLoading(false);
      })
      .catch((err) => {
        if (!active) {
          return;
        }
        setError(err.message || "Failed to load dashboard data.");
        setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [loadStats]);

  useEffect(() => {
    if (loading) {
      return;
    }

    loadPeople().catch((err) => {
      setError(err.message || "Failed to load contacts.");
    });
  }, [loadPeople, loading]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      await Promise.all([loadStats(), loadPeople()]);
      setError("");
    } catch (err) {
      setError(err.message || "Failed to refresh dashboard data.");
    } finally {
      setRefreshing(false);
    }
  };

  const handleSort = (col) => {
    if (sortCol === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortCol(col); setSortDir("asc"); }
    setPage(0);
  };

  if (loading) return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center">
      <div className="text-gray-400 text-lg">Loading GT Machine...</div>
    </div>
  );

  const totalPages = Math.ceil(peopleCount / PAGE_SIZE);
  const jobsEmptyState = getJobsEmptyState(jobs);

  if (error) return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center px-6">
      <div className="max-w-lg rounded-2xl border border-red-900/50 bg-red-950/20 p-6 text-center">
        <div className="text-lg font-semibold text-red-200">Dashboard load failed</div>
        <p className="mt-2 text-sm text-red-300">{error}</p>
        <button
          onClick={refresh}
          className="mt-4 rounded-lg bg-red-900/40 px-4 py-2 text-sm font-medium text-red-100 hover:bg-red-900/60"
        >
          Retry
        </button>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center font-bold text-sm">GT</div>
            <h1 className="text-lg font-semibold text-white">GT Machine</h1>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={refresh} className={`p-2 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white transition-colors ${refreshing ? "animate-spin" : ""}`}>
              <RefreshCw size={16} />
            </button>
            <nav className="flex gap-1 bg-gray-800/50 rounded-lg p-1">
              {["overview", "contacts", "jobs"].map(t => (
                <button key={t} onClick={() => { setTab(t); setPage(0); }}
                  className={`px-4 py-1.5 rounded-md text-sm font-medium transition-colors ${tab === t ? "bg-gray-700 text-white" : "text-gray-400 hover:text-gray-200"}`}>
                  {t.charAt(0).toUpperCase() + t.slice(1)}
                </button>
              ))}
            </nav>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-6 py-6">
        {/* OVERVIEW TAB */}
        {tab === "overview" && (
          <div className="space-y-6">
            {/* Stat cards */}
            <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
              <StatCard label="Total Contacts" value={stats.total} icon={<Users size={18} />} color="blue" />
              <StatCard label="Queued" value={stats.queued} icon={<AlertCircle size={18} />} color="yellow" />
              <StatCard label="Enriched" value={stats.enriched} icon={<Users size={18} />} color="purple" />
              <StatCard label="Drafted" value={stats.drafted} icon={<Briefcase size={18} />} color="cyan" />
              <StatCard label="Sent" value={stats.sent} icon={<ExternalLink size={18} />} color="green" />
            </div>

            {/* Two column: Recent contacts + Jobs/Opportunities */}
            <div className="grid lg:grid-cols-2 gap-6">
              {/* Recently Added */}
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Recently Added</h2>
                  <button onClick={() => setTab("contacts")} className="text-xs text-blue-400 hover:text-blue-300">View all</button>
                </div>
                <div className="space-y-2">
                  {recentPeople.length === 0 && <p className="text-gray-500 text-sm">No contacts yet.</p>}
                  {recentPeople.map(p => (
                    <div key={p.id} className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-800/50 transition-colors">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-gray-200 truncate">{p.name || "Unknown"}</div>
                        <div className="text-xs text-gray-500 truncate">{p.title}{p.title && p.company_name ? " at " : ""}{p.company_name}</div>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <StatusBadge status={p.status} />
                        {p.linkedin_url && (
                          <a href={p.linkedin_url.startsWith("http") ? p.linkedin_url : `https://linkedin.com/in/${p.linkedin_url}`}
                            target="_blank" rel="noopener noreferrer" className="text-gray-500 hover:text-blue-400">
                            <ExternalLink size={12} />
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Open Jobs / Opportunities */}
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Open Roles (DNA Fit)</h2>
                  <button onClick={() => setTab("jobs")} className="text-xs text-blue-400 hover:text-blue-300">View all</button>
                </div>
                <div className="space-y-2">
                  {jobsEmptyState && <p className="text-gray-500 text-sm">{jobsEmptyState}</p>}
                  {jobs.filter(j => j.dna_fit).slice(0, 8).map(j => (
                    <div key={j.id} className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-800/50 transition-colors">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-gray-200 truncate">{j.title}</div>
                        <div className="text-xs text-gray-500 truncate">
                          {j.company_name}
                          {j.warm_contact_name && <span className="text-amber-400 ml-2">Warm: {j.warm_contact_name}</span>}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <StatusBadge status={j.status} />
                        {j.url && (
                          <a href={j.url} target="_blank" rel="noopener noreferrer" className="text-gray-500 hover:text-blue-400">
                            <ExternalLink size={12} />
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* High Priority Reach Outs */}
            <div className="bg-gray-900 border border-amber-900/50 rounded-xl p-5">
              <h2 className="text-sm font-semibold text-amber-400 uppercase tracking-wide mb-3">High Priority Reach Outs</h2>
              <p className="text-gray-400 text-sm mb-4">
                Contacts linked to DNA-fit open roles, recent funding, or job changes. This section populates as enrichment and trigger scenarios (S2, S5, S10, S11) come online.
              </p>
              <div className="space-y-2">
                {jobs.filter(j => j.dna_fit && j.warm_contact_name).map(j => (
                  <div key={j.id} className="flex items-center justify-between py-3 px-4 rounded-lg bg-amber-950/20 border border-amber-900/30">
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-amber-200">{j.warm_contact_name}</div>
                      <div className="text-xs text-gray-400">
                        Warm path to <span className="text-gray-200">{j.title}</span> at <span className="text-gray-200">{j.company_name}</span>
                      </div>
                    </div>
                    <div className="flex gap-2">
                      {j.url && (
                        <a href={j.url} target="_blank" rel="noopener noreferrer"
                          className="text-xs px-3 py-1 rounded-md bg-amber-900/40 text-amber-300 hover:bg-amber-900/60 transition-colors">
                          View Role
                        </a>
                      )}
                    </div>
                  </div>
                ))}
                {jobs.filter(j => j.dna_fit && j.warm_contact_name).length === 0 && (
                  <div className="text-center py-6 text-gray-600 text-sm">
                    No warm contact opportunities found yet. As more data flows in, priority reach outs will appear here.
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* CONTACTS TAB */}
        {tab === "contacts" && (
          <div className="space-y-4">
            {/* Search + Filters */}
            <div className="flex flex-col sm:flex-row gap-3">
              <div className="relative flex-1">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
                <input type="text" placeholder="Search name, company, title, or email..."
                  value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}
                  className="w-full pl-10 pr-4 py-2 bg-gray-900 border border-gray-800 rounded-lg text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600"
                />
                {search && (
                  <button onClick={() => { setSearch(""); setPage(0); }} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300">
                    <X size={14} />
                  </button>
                )}
              </div>
              <div className="flex items-center gap-2">
                <Filter size={14} className="text-gray-500" />
                {["all", "queued", "enriched", "drafted", "sent", "archived"].map(s => (
                  <button key={s} onClick={() => { setStatusFilter(s); setPage(0); }}
                    className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${statusFilter === s ? "bg-blue-600/20 text-blue-400 border border-blue-600/40" : "bg-gray-800 text-gray-400 hover:text-gray-200 border border-transparent"}`}>
                    {s.charAt(0).toUpperCase() + s.slice(1)}
                  </button>
                ))}
              </div>
            </div>

            {/* Count */}
            <div className="text-xs text-gray-500">{peopleCount.toLocaleString()} contacts found</div>

            {/* Table */}
            <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-800">
                      {[
                        { key: "name", label: "Name" },
                        { key: "title", label: "Title" },
                        { key: "company_name", label: "Company" },
                        { key: "email", label: "Email" },
                        { key: "status", label: "Status" },
                        { key: "created_at", label: "Added" },
                      ].map(col => (
                        <th key={col.key} onClick={() => handleSort(col.key)}
                          className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide cursor-pointer hover:text-gray-200 select-none">
                          <span className="flex items-center gap-1">
                            {col.label}
                            {sortCol === col.key && (sortDir === "asc" ? <ChevronUp size={12} /> : <ChevronDown size={12} />)}
                          </span>
                        </th>
                      ))}
                      <th className="px-4 py-3 w-10"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800/50">
                    {people.map(p => (
                      <tr key={p.id} className="hover:bg-gray-800/30 transition-colors">
                        <td className="px-4 py-3 font-medium text-gray-200">{p.name || "Unknown"}</td>
                        <td className="px-4 py-3 text-gray-400">{p.title || ""}</td>
                        <td className="px-4 py-3 text-gray-400">{p.company_name || ""}</td>
                        <td className="px-4 py-3 text-gray-400 truncate max-w-xs">{p.email || ""}</td>
                        <td className="px-4 py-3"><StatusBadge status={p.status} /></td>
                        <td className="px-4 py-3 text-gray-500 text-xs">{p.created_at ? new Date(p.created_at).toLocaleDateString() : ""}</td>
                        <td className="px-4 py-3">
                          {p.linkedin_url && (
                            <a href={p.linkedin_url.startsWith("http") ? p.linkedin_url : `https://linkedin.com/in/${p.linkedin_url}`}
                              target="_blank" rel="noopener noreferrer" className="text-gray-500 hover:text-blue-400">
                              <ExternalLink size={14} />
                            </a>
                          )}
                        </td>
                      </tr>
                    ))}
                    {people.length === 0 && (
                      <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-600">No contacts match your search.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between">
                <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}
                  className="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed">
                  Previous
                </button>
                <span className="text-sm text-gray-500">Page {page + 1} of {totalPages}</span>
                <button onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))} disabled={page >= totalPages - 1}
                  className="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-300 hover:bg-gray-700 disabled:opacity-30 disabled:cursor-not-allowed">
                  Next
                </button>
              </div>
            )}
          </div>
        )}

        {/* JOBS TAB */}
        {tab === "jobs" && (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-gray-200">Tracked Roles</h2>
            <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-800">
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Title</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Company</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Warm Contact</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">DNA Fit</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">Status</th>
                      <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wide">First Seen</th>
                      <th className="px-4 py-3 w-10"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800/50">
                    {jobs.map(j => (
                      <tr key={j.id} className="hover:bg-gray-800/30 transition-colors">
                        <td className="px-4 py-3 font-medium text-gray-200">{j.title}</td>
                        <td className="px-4 py-3 text-gray-400">{j.company_name}</td>
                        <td className="px-4 py-3 text-amber-400">{j.warm_contact_name || ""}</td>
                        <td className="px-4 py-3">{j.dna_fit ? <span className="text-green-400 text-xs font-medium">Yes</span> : <span className="text-gray-600 text-xs">No</span>}</td>
                        <td className="px-4 py-3"><StatusBadge status={j.status} /></td>
                        <td className="px-4 py-3 text-gray-500 text-xs">{j.first_seen ? new Date(j.first_seen).toLocaleDateString() : ""}</td>
                        <td className="px-4 py-3">
                          {j.url && (
                            <a href={j.url} target="_blank" rel="noopener noreferrer" className="text-gray-500 hover:text-blue-400">
                              <ExternalLink size={14} />
                            </a>
                          )}
                        </td>
                      </tr>
                    ))}
                    {jobs.length === 0 && (
                      <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-600">No jobs tracked yet.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

// ─── COMPONENTS ──────────────────────────────────────

function StatCard({ label, value, icon, color }) {
  const colors = {
    blue: "text-blue-400 bg-blue-950/50 border-blue-900/40",
    yellow: "text-yellow-400 bg-yellow-950/50 border-yellow-900/40",
    purple: "text-purple-400 bg-purple-950/50 border-purple-900/40",
    cyan: "text-cyan-400 bg-cyan-950/50 border-cyan-900/40",
    green: "text-green-400 bg-green-950/50 border-green-900/40",
  };
  return (
    <div className={`rounded-xl border p-4 ${colors[color]}`}>
      <div className="flex items-center gap-2 mb-2 opacity-70">{icon}<span className="text-xs font-medium uppercase tracking-wide">{label}</span></div>
      <div className="text-2xl font-bold">{(value || 0).toLocaleString()}</div>
    </div>
  );
}

function StatusBadge({ status }) {
  const styles = {
    queued: "bg-yellow-900/30 text-yellow-400 border-yellow-800/40",
    enriched: "bg-purple-900/30 text-purple-400 border-purple-800/40",
    drafted: "bg-cyan-900/30 text-cyan-400 border-cyan-800/40",
    sent: "bg-green-900/30 text-green-400 border-green-800/40",
    archived: "bg-gray-800/50 text-gray-500 border-gray-700/40",
    new: "bg-blue-900/30 text-blue-400 border-blue-800/40",
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded-md text-xs font-medium border ${styles[status] || styles.new}`}>
      {status || "unknown"}
    </span>
  );
}
