import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { applyPick, applyTheme, readPick, readTheme } from "../lib/theme";
import { applyCustomPick, applyDensity, applyFont, applyGrain, applyHazeStrength, applyMotion, readDensity, readFont, readGrain, readHazeStrength, readMotion } from "./appearance";
import { consoleQueryClient } from "./fleet";
import { ConsoleApp } from "./router";
import "./theme.css";

applyTheme(readTheme());
applyPick("light", readPick("light"));
applyPick("dark", readPick("dark"));
applyCustomPick();
applyFont(readFont());
applyDensity(readDensity());
applyMotion(readMotion());
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
