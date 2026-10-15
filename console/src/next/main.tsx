import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { applyTheme, readTheme } from "../lib/theme";
import { NextConsole } from "./router";
import "./theme.css";

applyTheme(readTheme());

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from next.html");
}
createRoot(root).render(
  <StrictMode>
    <NextConsole />
  </StrictMode>,
);
