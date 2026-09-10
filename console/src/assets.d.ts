declare module "*.svg" {
  const path: string;
  export default path;
}

// `import "./theme.css"` in main.tsx is a side effect for the bundler; the
// compiler wants a declaration for it (TS2882, on by default from TypeScript 7).
declare module "*.css";
