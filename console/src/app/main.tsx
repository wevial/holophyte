import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { applyPick, applyTheme, readPick, readTheme } from "../lib/theme";
import { applyDensity, applyFont, applyGrain, applyHazeStrength, readDensity, readFont, readGrain, readHazeStrength } from "./appearance";
import { consoleQueryClient } from "./fleet";
import { ConsoleApp } from "./router";
import "./theme.css";

applyTheme(readTheme());
applyPick("light", readPick("light"));
applyPick("dark", readPick("dark"));
applyFont(readFont());
applyDensity(readDensity());
applyGrain(readGrain());
applyHazeStrength(readHazeStrength());

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from index.html");
}
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp />
    </QueryClientProvider>
  </StrictMode>,
);
