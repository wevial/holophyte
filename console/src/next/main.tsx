import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { applyTheme, readTheme } from "../lib/theme";
import { applyFont, readFont } from "./appearance";
import { consoleQueryClient } from "./fleet";
import { NextConsole } from "./router";
import "./theme.css";

applyTheme(readTheme());
applyFont(readFont());

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from next.html");
}
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={consoleQueryClient()}>
      <NextConsole />
    </QueryClientProvider>
  </StrictMode>,
);
