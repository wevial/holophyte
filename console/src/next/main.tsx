import { createRoot } from "react-dom/client";
import { StrictMode } from "react";
import { applyTheme, readTheme } from "../lib/theme";
import "./theme.css";

applyTheme(readTheme());

const root = document.getElementById("root");
if (!root) {
  throw new Error("console: #root mount point is missing from next.html");
}
createRoot(root).render(
  <StrictMode>
    <main>
      <h1>Holophyte</h1>
      <p>The new console is being built here. Until it is ready, the classic console does everything.</p>
      <a href="/">Open the classic console</a>
    </main>
  </StrictMode>,
);
