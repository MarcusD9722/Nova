import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles/theme.css";
import "./styles/index.css";

// One global auth injection point instead of touching every fetch() call site
// (Phase 0.3). No token configured = no behavior change.
import { installAuthFetch } from "./lib/apiToken";

installAuthFetch();

// Remove StrictMode to avoid React dev double-mounting the R3F Canvas
ReactDOM.createRoot(document.getElementById("root")).render(
  // <React.StrictMode>
    <App />
  // </React.StrictMode>
);
