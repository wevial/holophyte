import { createRoot, type Root } from "react-dom/client";
import { StrictMode } from "react";
import { App } from "./App";

/** Render the classic console into `root`. */
export function startClassic(root: HTMLElement): Root {
  const app = createRoot(root);
  app.render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
  return app;
}
