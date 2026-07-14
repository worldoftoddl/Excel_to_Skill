import { describe, expect, it } from "vitest";

import type { ApplyManifest } from "../src/executor/contracts";
import {
  OfficeJsWorkbookPort,
  type OfficeJsWorkbookPortOptions,
} from "../src/executor/office-port";
import {
  BLANK_STATE,
  clone,
  MANIFEST,
  VALUE_STATE,
} from "./fixtures";

interface FakeCell {
  value: string | number | boolean;
  formula: string | null;
  valueType: string;
  numberFormat: string;
  text: string;
  merged: boolean;
  spill: "none" | "parent" | "child";
  table: boolean;
  locked: boolean;
  savedAsArray: boolean;
}

class FakeRequestQueue {
  readonly operations: Array<() => void> = [];

  enqueue(operation: () => void): void {
    this.operations.push(operation);
  }

  flush(): void {
    const pending = this.operations.splice(0);
    for (const operation of pending) operation();
  }
}

class FakeRange {
  readonly format: { protection: { locked: boolean; load: () => void } };
  readonly #cell: FakeCell;
  readonly #queue: FakeRequestQueue;

  constructor(cell: FakeCell, queue: FakeRequestQueue) {
    this.#cell = cell;
    this.#queue = queue;
    this.format = {
      protection: {
        locked: cell.locked,
        load: () => undefined,
      },
    };
  }

  load(): this {
    return this;
  }

  get values(): Array<Array<string | number | boolean>> {
    return [[this.#cell.value]];
  }

  set values(value: Array<Array<string | number | boolean>>) {
    const scalar = value[0]![0]!;
    this.#queue.enqueue(() => {
      this.#cell.value = scalar;
      this.#cell.formula = null;
      this.#cell.valueType = valueType(scalar);
      this.#cell.text = String(scalar);
    });
  }

  get formulas(): Array<Array<string | number | boolean>> {
    return [[this.#cell.formula ?? this.#cell.value]];
  }

  set formulas(value: Array<Array<string | number | boolean>>) {
    const formula = value[0]![0];
    if (typeof formula !== "string") throw new Error("formula expected");
    this.#queue.enqueue(() => {
      this.#cell.formula = formula;
      this.#cell.value = 3;
      this.#cell.valueType = "Double";
      this.#cell.text = "3";
    });
  }

  get valueTypes(): Array<Array<string>> {
    return [[this.#cell.valueType]];
  }

  get numberFormat(): string[][] {
    return [[this.#cell.numberFormat]];
  }

  set numberFormat(value: string[][]) {
    const numberFormat = value[0]![0]!;
    this.#queue.enqueue(() => {
      this.#cell.numberFormat = numberFormat;
    });
  }

  get text(): string[][] {
    return [[this.#cell.text]];
  }

  get savedAsArray(): boolean {
    return this.#cell.savedAsArray;
  }

  getMergedAreasOrNullObject(): FakeNullProxy {
    return new FakeNullProxy(!this.#cell.merged);
  }

  getSpillParentOrNullObject(): FakeNullProxy {
    return new FakeNullProxy(this.#cell.spill !== "child");
  }

  getSpillingToRangeOrNullObject(): FakeNullProxy {
    return new FakeNullProxy(this.#cell.spill !== "parent");
  }

  getSpecialCellsOrNullObject(): FakeNullProxy {
    return new FakeNullProxy(this.#cell.formula === null);
  }

  getTables(): { items: unknown[]; load: () => void } {
    return { items: this.#cell.table ? [{}] : [], load: () => undefined };
  }

  clear(): void {
    this.#queue.enqueue(() => {
      this.#cell.value = "";
      this.#cell.formula = null;
      this.#cell.valueType = "Empty";
      this.#cell.text = "";
    });
  }
}

class FakeNullProxy {
  readonly isNullObject: boolean;
  readonly address = "C!A1";

  constructor(isNullObject: boolean) {
    this.isNullObject = isNullObject;
  }

  load(): this {
    return this;
  }
}

class FakeWorksheet {
  id = "worksheet-a";
  name = "C";
  readonly protection: { protected: boolean; load: () => void };
  enableCalculation = true;
  calculateCalls = 0;
  lastCalculateMarkAllDirty: boolean | undefined;
  readonly #cells: Map<string, FakeCell>;
  readonly #queue: FakeRequestQueue;

  constructor(cells: Map<string, FakeCell>, queue: FakeRequestQueue, protectedSheet = false) {
    this.#cells = cells;
    this.#queue = queue;
    this.protection = { protected: protectedSheet, load: () => undefined };
  }

  load(): this {
    return this;
  }

  getRange(cell: string): FakeRange {
    const found = this.#cells.get(cell);
    if (found === undefined) throw new Error(`missing cell ${cell}`);
    return new FakeRange(found, this.#queue);
  }

  calculate(markAllDirty?: boolean): void {
    this.#queue.enqueue(() => {
      this.calculateCalls += 1;
      this.lastCalculateMarkAllDirty = markAllDirty;
    });
  }
}

class FakeContext {
  readonly workbook: {
    worksheets: { getItem: () => FakeWorksheet };
    save: () => void;
    load: () => void;
    readOnly: boolean;
  };
  syncCalls = 0;
  failSyncAt: number | null = null;
  saved = false;
  readonly #queue: FakeRequestQueue;

  constructor(worksheet: FakeWorksheet, queue: FakeRequestQueue, readOnly = false) {
    this.#queue = queue;
    this.workbook = {
      worksheets: { getItem: () => worksheet },
      save: () => {
        this.#queue.enqueue(() => {
          this.saved = true;
        });
      },
      load: () => undefined,
      readOnly,
    };
  }

  async sync(): Promise<void> {
    this.syncCalls += 1;
    if (this.syncCalls === this.failSyncAt) throw new Error("sync failed");
    this.#queue.flush();
  }
}

describe("OfficeJsWorkbookPort", () => {
  it("reads a canonical blank single-cell state and all safety constraints", async () => {
    const harness = fakeHarness();
    const result = await harness.port.readStates("C", "worksheet-a", ["A1"]);
    expect(result).toEqual([BLANK_STATE]);
  });

  it("applies only the immutable value edit after write-start confirmation", async () => {
    const harness = fakeHarness();
    let starts = 0;
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      starts += 1;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });

    expect(starts).toBe(1);
    expect(witness.outcome).toBe("applied");
    expect(witness.actual_after).toEqual([VALUE_STATE]);
    expect(harness.cells.get("A1")?.value).toBe("승인됨");
  });

  it("does not start or write when the exact before state drifted", async () => {
    const harness = fakeHarness({ value: "drift", valueType: "String", text: "drift" });
    let starts = 0;
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      starts += 1;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });

    expect(witness.outcome).toBe("stale_precondition");
    expect(starts).toBe(0);
    expect(harness.cells.get("A1")?.value).toBe("drift");
  });

  it("recalculates a formula edit and rereads its calculated value", async () => {
    const harness = fakeHarness();
    const manifest = formulaManifest();
    const witness = await harness.port.executeManifest(manifest, async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));

    expect(witness.outcome).toBe("applied");
    expect(witness.recalculation).toBe("recalculate");
    expect(harness.worksheet.calculateCalls).toBe(1);
    expect(harness.worksheet.lastCalculateMarkAllDirty).toBe(true);
    expect(witness.actual_after?.[0]?.authored).toEqual({ kind: "formula", formula: "=1+2" });
    expect(witness.actual_after?.[0]?.calculated_value).toBe(3);
  });

  it("returns indeterminate when an Office sync fails after start", async () => {
    const harness = fakeHarness();
    harness.context.failSyncAt = 2;
    const witness = await harness.port.executeManifest(MANIFEST, async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
  });

  it("returns indeterminate when the queued write sync fails", async () => {
    const harness = fakeHarness();
    harness.context.failSyncAt = 3;
    const witness = await harness.port.executeManifest(MANIFEST, async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.value).toBe("");
  });

  it("returns indeterminate if final reread sync fails after the queued write applied", async () => {
    const harness = fakeHarness();
    harness.context.failSyncAt = 4;
    const witness = await harness.port.executeManifest(MANIFEST, async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.value).toBe("승인됨");
  });

  it("returns indeterminate if worksheet recalculation sync fails", async () => {
    const harness = fakeHarness();
    harness.context.failSyncAt = 4;
    const witness = await harness.port.executeManifest(formulaManifest(), async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.formula).toBe("=1+2");
  });

  it("does not write when the execution lease expires during the post-start reread", async () => {
    let clockReads = 0;
    const harness = fakeHarness({}, false, () => {
      clockReads += 1;
      return clockReads === 1
        ? Date.parse("2099-07-14T00:09:59Z")
        : Date.parse("2099-07-14T00:10:01Z");
    });
    const witness = await harness.port.executeManifest(MANIFEST, async () => ({
      executionDeadline: "2099-07-14T00:10:00Z",
    }));

    expect(witness.outcome).toBe("indeterminate");
    expect(harness.cells.get("A1")?.value).toBe("");
  });

  it("fails closed on a protected worksheet", async () => {
    const harness = fakeHarness({}, true);
    const states = await harness.port.readStates("C", "worksheet-a", ["A1"]);
    expect(states[0]?.target_constraints.protected).toBe(true);
    let starts = 0;
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      starts += 1;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness.outcome).toBe("stale_precondition");
    expect(starts).toBe(0);
  });

  it.each([
    ["merged", { merged: true }],
    ["spill parent", { spill: "parent" as const }],
    ["spill child", { spill: "child" as const }],
    ["table member", { table: true }],
  ])("does not start an approved edit after the target becomes %s", async (_label, override) => {
    const harness = fakeHarness(override);
    let starts = 0;
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      starts += 1;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness.outcome).toBe("stale_precondition");
    expect(starts).toBe(0);
    expect(harness.cells.get("A1")?.value).toBe("");
  });

  it("classifies a legacy array-formula target as unsafe before write-start", async () => {
    const harness = fakeHarness({ savedAsArray: true });
    const states = await harness.port.readStates("C", "worksheet-a", ["A1"]);
    expect(states[0]?.target_constraints.spill).toBe("parent");

    let starts = 0;
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      starts += 1;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness.outcome).toBe("stale_precondition");
    expect(starts).toBe(0);
  });

  it("fails before write-start on a read-only workbook", async () => {
    const harness = fakeHarness({}, false, undefined, true);
    let starts = 0;
    await expect(
      harness.port.executeManifest(MANIFEST, async () => {
        starts += 1;
        return { executionDeadline: "2099-07-14T00:10:00Z" };
      }),
    ).rejects.toMatchObject({ name: "OfficeBeforeWriteError" });
    expect(starts).toBe(0);
  });

  it("fails before write-start when formula calculation is disabled", async () => {
    const harness = fakeHarness();
    harness.worksheet.enableCalculation = false;
    let starts = 0;
    await expect(
      harness.port.executeManifest(formulaManifest(), async () => {
        starts += 1;
        return { executionDeadline: "2099-07-14T00:10:00Z" };
      }),
    ).rejects.toMatchObject({ name: "OfficeBeforeWriteError" });
    expect(starts).toBe(0);
  });

  it("returns indeterminate when the worksheet binding changes after write-start", async () => {
    const harness = fakeHarness();
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      harness.worksheet.name = "Renamed";
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.value).toBe("");
  });

  it("returns indeterminate when the workbook becomes read-only after write-start", async () => {
    const harness = fakeHarness();
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      harness.context.workbook.readOnly = true;
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.value).toBe("");
  });

  it("prevents a coauthor value change from being overwritten after write-start", async () => {
    const harness = fakeHarness();
    const witness = await harness.port.executeManifest(MANIFEST, async () => {
      const cell = harness.cells.get("A1")!;
      cell.value = "coauthor";
      cell.valueType = "String";
      cell.text = "coauthor";
      return { executionDeadline: "2099-07-14T00:10:00Z" };
    });
    expect(witness).toMatchObject({ outcome: "indeterminate", actual_after: null });
    expect(harness.cells.get("A1")?.value).toBe("coauthor");
  });

  it("does not infer a formula from a formula-looking literal cell", async () => {
    const harness = fakeHarness({
      value: "=literal",
      formula: null,
      valueType: "String",
      text: "=literal",
    });
    const states = await harness.port.readStates("C", "worksheet-a", ["A1"]);
    expect(states[0]?.authored).toEqual({ kind: "value", value: "=literal" });
  });

  it("saves only through the explicit workbook save API", async () => {
    const harness = fakeHarness();
    await harness.port.saveCurrentWorkbook();
    expect(harness.context.saved).toBe(true);
  });

  it("propagates a queued workbook save sync failure", async () => {
    const harness = fakeHarness();
    harness.context.failSyncAt = 1;
    await expect(harness.port.saveCurrentWorkbook()).rejects.toThrow("sync failed");
    expect(harness.context.saved).toBe(false);
  });

  it("rejects Excel hosts below the declared 1.13 requirement set", () => {
    const harness = fakeHarness();
    const unsupported = new OfficeJsWorkbookPort({
      excelRun: async <T>(batch: (request: Excel.RequestContext) => Promise<T>) =>
        batch(harness.context as unknown as Excel.RequestContext),
      office: {
        context: { requirements: { isSetSupported: () => false } },
      } as unknown as typeof Office,
      excel: {} as typeof Excel,
    });
    expect(() => unsupported.assertSupported()).toThrow(/ExcelApi 1.13/);
  });
});

function fakeHarness(
  override: Partial<FakeCell> = {},
  protectedSheet = false,
  now: () => number = () => Date.parse("2026-07-14T00:00:00Z"),
  readOnly = false,
): {
  cells: Map<string, FakeCell>;
  context: FakeContext;
  worksheet: FakeWorksheet;
  port: OfficeJsWorkbookPort;
} {
  const cell: FakeCell = {
    value: "",
    formula: null,
    valueType: "Empty",
    numberFormat: "General",
    text: "",
    merged: false,
    spill: "none",
    table: false,
    locked: true,
    savedAsArray: false,
    ...override,
  };
  const cells = new Map([["A1", cell]]);
  const queue = new FakeRequestQueue();
  const worksheet = new FakeWorksheet(cells, queue, protectedSheet);
  const context = new FakeContext(worksheet, queue, readOnly);
  const options: OfficeJsWorkbookPortOptions = {
    excelRun: async <T>(batch: (request: Excel.RequestContext) => Promise<T>) =>
      batch(context as unknown as Excel.RequestContext),
    office: {
      context: {
        requirements: { isSetSupported: () => true },
      },
    } as unknown as typeof Office,
    excel: {
      run: () => undefined,
      CalculationType: { recalculate: "Recalculate" },
      CalculationState: { done: "Done" },
      SaveBehavior: { save: "Save" },
      ClearApplyTo: { contents: "Contents" },
    } as unknown as typeof Excel,
    now,
  };
  return { cells, context, worksheet, port: new OfficeJsWorkbookPort(options) };
}

function formulaManifest(): ApplyManifest {
  const manifest = clone(MANIFEST);
  manifest.diff = [
    {
      ...manifest.diff[0]!,
      kind: "set_formula",
      after: {
        cell: "A1",
        authored: { kind: "formula", formula: "=1+2" },
        number_format: "General",
      },
    },
  ];
  manifest.expected_after = [manifest.diff[0]!.after];
  return manifest;
}

function valueType(value: string | number | boolean): string {
  if (typeof value === "string") return "String";
  if (typeof value === "number") return "Double";
  return "Boolean";
}
