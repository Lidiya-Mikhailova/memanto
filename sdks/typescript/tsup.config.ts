import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts", "src/integrations/ai-sdk.ts"],
  format: ["cjs", "esm"],
  dts: true,
  sourcemap: true,
  clean: true,
  splitting: false,
  target: "node18",
  outDir: "dist",
});
