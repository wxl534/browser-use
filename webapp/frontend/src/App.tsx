import { NavLink, Navigate, Route, Routes } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { api } from "./api"
import Dashboard from "./views/Dashboard"
import Gallery from "./views/Gallery"
import Records from "./views/Records"
import Runs from "./views/Runs"
import Control from "./views/Control"

const NAV = [
  { to: "/dashboard", label: "仪表盘", icon: "📊" },
  { to: "/gallery", label: "图片画廊", icon: "🖼️" },
  { to: "/records", label: "元数据表", icon: "🗂️" },
  { to: "/runs", label: "运行历史", icon: "🕓" },
  { to: "/control", label: "控制台", icon: "🎛️" },
]

export default function App() {
  const status = useQuery({
    queryKey: ["run-status"],
    queryFn: api.runStatus,
    refetchInterval: 3000,
  })
  const running = status.data?.running ?? false

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">IDP</span>
          <span className="brand-text">爬虫控制台</span>
        </div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
              <span className="nav-icon">{n.icon}</span>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={"dot " + (running ? "dot-run" : "dot-idle")} />
          {running ? "任务运行中" : "空闲"}
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/gallery" element={<Gallery />} />
          <Route path="/records" element={<Records />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/control" element={<Control />} />
        </Routes>
      </main>
    </div>
  )
}
