import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { App } from "./App";
import "./theme.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from index.html");
}
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
