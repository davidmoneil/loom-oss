import { useEffect } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { api, setDisplayTimezone } from "../api.js";

const NAV = [
  { to: "/", label: "Overview", end: true, icon: GridIcon },
  { to: "/metrics", label: "Metrics", icon: ChartIcon },
  { to: "/audit", label: "Audit", icon: ListIcon },
  { to: "/scanner", label: "Data Protection", icon: ShieldIcon },
  { to: "/governor", label: "Governor", icon: GaugeIcon },
  { to: "/rate-limits", label: "Rate Limits", icon: SpeedIcon },
  { to: "/costs", label: "Costs", icon: DollarIcon },
  { to: "/sessions", label: "Sessions", icon: UsersIcon },
  { to: "/routing", label: "Routing", icon: RouteIcon },
];

export default function Layout() {
  useEffect(() => {
    api.config().then((cfg) => {
      const tz = cfg?.server?.display_timezone;
      if (tz) setDisplayTimezone(tz);
    }).catch(() => {});
  }, []);

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-16 shrink-0 flex-col border-r border-border bg-card md:w-56">
        <div className="flex h-16 items-center gap-2 border-b border-border px-4">
          <div className="flex h-8 w-8 items-center justify-center rounded bg-accent font-bold text-white">
            L
          </div>
          <span className="hidden text-lg font-semibold text-white md:inline">
            Loom
          </span>
        </div>
        <nav className="flex flex-1 flex-col gap-1 p-2">
          {NAV.map(({ to, label, end, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-accent text-white"
                    : "text-gray-400 hover:bg-gray-700/50 hover:text-white"
                }`
              }
            >
              <Icon />
              <span className="hidden md:inline">{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="hidden border-t border-border p-3 text-xs text-gray-500 md:block">
          LLM optimization gateway
        </div>
      </aside>
      <main className="flex-1 overflow-x-hidden">
        <Outlet />
      </main>
    </div>
  );
}

function GaugeIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" />
      <path d="M19.4 15a7.97 7.97 0 0 0 0-6M4.6 9a7.97 7.97 0 0 0 0 6" />
      <path d="M12 3v3M4.9 4.9l2.1 2.1M19.1 4.9 17 7" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  );
}

function GridIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </svg>
  );
}

function ChartIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M3 3v18h18" />
      <path d="M7 14l3-4 3 3 4-6" />
    </svg>
  );
}

function ListIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <line x1="8" y1="6" x2="21" y2="6" />
      <line x1="8" y1="12" x2="21" y2="12" />
      <line x1="8" y1="18" x2="21" y2="18" />
      <circle cx="3.5" cy="6" r="1" />
      <circle cx="3.5" cy="12" r="1" />
      <circle cx="3.5" cy="18" r="1" />
    </svg>
  );
}

function SpeedIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M12 12m-10 0a10 10 0 1 0 20 0" />
      <path d="M12 12l3.5-6" />
      <circle cx="12" cy="12" r="1.5" />
    </svg>
  );
}

function DollarIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
    </svg>
  );
}

function UsersIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

function RouteIcon() {
  return (
    <svg className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <circle cx="6" cy="19" r="3" />
      <circle cx="18" cy="5" r="3" />
      <path d="M12 19h4.5a3.5 3.5 0 0 0 0-7h-9a3.5 3.5 0 0 1 0-7H12" />
    </svg>
  );
}
