import { getHttpsServerOptions } from "office-addin-dev-certs";
import { defineConfig } from "vite";

export default defineConfig(async ({ command, mode }) => {
  const https = command === "serve" && mode !== "test"
    ? await getHttpsServerOptions()
    : undefined;
  return {
    build: {
      rollupOptions: {
        input: "taskpane.html",
      },
    },
    server: {
      port: 3000,
      strictPort: true,
      ...(https === undefined ? {} : { https }),
    },
  };
});
