import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  {
    // The same limits as the backend's ruff settings.
    rules: {
      complexity: ["error", 12],
      "max-statements": ["error", 50],
    },
  },
  {
    // A disable comment that no longer suppresses anything fails the check.
    linterOptions: { reportUnusedDisableDirectives: "error" },
  },
  // openapi-typescript generates app/openapi.ts, so it is not edited by hand.
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts", "app/openapi.ts"]),
]);
