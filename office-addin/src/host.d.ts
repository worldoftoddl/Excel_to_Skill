import type { WorkbookEditHostConfig } from "./host-bootstrap";

declare global {
  interface Window {
    auditWorkbookEditHost?: WorkbookEditHostConfig;
  }
}

export {};
