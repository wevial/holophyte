import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { App } from "./App";
import { applyTheme, readTheme } from "./lib/theme";
import "./theme.css";

// Stamp the persisted theme before first paint so the page never flashes
// the other set.
applyTheme(readTheme());

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from index.html");
}
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
