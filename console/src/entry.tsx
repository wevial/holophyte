import { createRoot, type Root } from "react-dom/client";
import { StrictMode } from "react";
import { App } from "./App";
import { openRememberedView } from "./lib/view";

/** Render the classic console into `root`, unless the remembered view
 *  replaces the page first; null then, with nothing rendered. */
export function startClassic(root: HTMLElement): Root | null {
  if (openRememberedView()) return null;
  const app = createRoot(root);
  app.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  return app;
}
