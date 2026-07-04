import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout.jsx";
import Overview from "./pages/Overview.jsx";
import Metrics from "./pages/Metrics.jsx";
import Audit from "./pages/Audit.jsx";
import Scanner from "./pages/Scanner.jsx";
import Governor from "./pages/Governor.jsx";
import RateLimits from "./pages/RateLimits.jsx";
import Costs from "./pages/Costs.jsx";
import Sessions from "./pages/Sessions.jsx";
import Routing from "./pages/Routing.jsx";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Overview />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/scanner" element={<Scanner />} />
          <Route path="/governor" element={<Governor />} />
          <Route path="/rate-limits" element={<RateLimits />} />
          <Route path="/costs" element={<Costs />} />
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/routing" element={<Routing />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </HashRouter>
  </React.StrictMode>
);
