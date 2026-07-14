import {
  type ApplyManifest,
  type CellState,
  type EditChange,
  type Scalar,
  type SpillState,
  type WitnessInput,
  WorkbookEditContractError,
} from "./contracts";

export interface StartedExecution {
  executionDeadline: string;
}

export interface WorkbookOfficePort {
  assertSupported(): void;
  readStates(sheet: string, worksheetId: string, cells: string[]): Promise<CellState[]>;
  executeManifest(
    manifest: ApplyManifest,
    markStarted: () => Promise<StartedExecution>,
  ): Promise<WitnessInput>;
  saveCurrentWorkbook(): Promise<void>;
}

export class OfficeBeforeWriteError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "OfficeBeforeWriteError";
  }
}

export class OfficeStartConfirmationError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "OfficeStartConfirmationError";
  }
}

export class OfficeStartRejectedError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "OfficeStartRejectedError";
  }
}

type ExcelRun = <T>(
  batch: (context: Excel.RequestContext) => Promise<T>,
) => Promise<T>;

interface CellReadHandle {
  cell: string;
  range: Excel.Range;
  merged: Excel.RangeAreas;
  spillParent: Excel.Range;
  spillingTo: Excel.Range;
  formulaCells: Excel.RangeAreas;
  tables: Excel.TableScopedCollection;
}

export interface OfficeJsWorkbookPortOptions {
  excelRun?: ExcelRun;
  office?: typeof Office;
  excel?: typeof Excel;
  now?: () => number;
}

export class OfficeJsWorkbookPort implements WorkbookOfficePort {
  readonly #excelRun: ExcelRun;
  readonly #office: typeof Office;
  readonly #excel: typeof Excel;
  readonly #now: () => number;

  constructor(options: OfficeJsWorkbookPortOptions = {}) {
    this.#excelRun = options.excelRun ?? Excel.run;
    this.#office = options.office ?? Office;
    this.#excel = options.excel ?? Excel;
    this.#now = options.now ?? Date.now;
  }

  assertSupported(): void {
    const supported = this.#office.context.requirements.isSetSupported("ExcelApi", "1.13");
    if (!supported) {
      throw new WorkbookEditContractError(
        "UNSUPPORTED_EXCEL_API",
        "이 편집기는 ExcelApi 1.13 이상이 필요합니다.",
      );
    }
  }

  async readStates(sheet: string, worksheetId: string, cells: string[]): Promise<CellState[]> {
    this.assertSupported();
    return this.#excelRun(async (context) => {
      const worksheet = context.workbook.worksheets.getItem(sheet);
      worksheet.load(["id", "name"]);
      const handles = prepareCellReads(worksheet, cells);
      await context.sync();
      assertWorksheet(worksheet, sheet, worksheetId);
      return materializeCellStates(worksheet, handles);
    });
  }

  async executeManifest(
    manifest: ApplyManifest,
    markStarted: () => Promise<StartedExecution>,
  ): Promise<WitnessInput> {
    this.assertSupported();
    let observedBefore: CellState[] | null = null;
    let startConfirmed = false;
    let recalculation: WitnessInput["recalculation"] = "none";
    try {
      return await this.#excelRun(async (context) => {
        const worksheet = context.workbook.worksheets.getItem(manifest.office_binding.sheet);
        context.workbook.load("readOnly");
        worksheet.load(["id", "name", "enableCalculation"]);
        const cells = manifest.before.map((item) => item.cell);
        const handles = prepareCellReads(worksheet, cells);
        await context.sync();
        assertWorksheet(
          worksheet,
          manifest.office_binding.sheet,
          manifest.office_binding.worksheet_id,
        );
        if (context.workbook.readOnly) {
          throw new WorkbookEditContractError(
            "READ_ONLY_WORKBOOK",
            "읽기 전용 workbook에는 승인된 변경을 적용할 수 없습니다.",
          );
        }
        if (
          manifest.diff.some((item) => item.kind === "set_formula") &&
          !worksheet.enableCalculation
        ) {
          throw new WorkbookEditContractError(
            "CALCULATION_DISABLED",
            "계산이 비활성화된 worksheet에는 formula edit를 적용할 수 없습니다.",
          );
        }
        observedBefore = materializeCellStates(worksheet, handles);

        if (!cellStatesEqual(observedBefore, manifest.before)) {
          return {
            outcome: "stale_precondition",
            observed_before: observedBefore,
            actual_after: null,
            recalculation: "none",
          };
        }

        let started: StartedExecution;
        try {
          started = await markStarted();
          startConfirmed = true;
        } catch (error) {
          if (error instanceof OfficeStartRejectedError) {
            throw error;
          }
          throw new OfficeStartConfirmationError(
            "write-start 확인 전에 API 호출이 실패했습니다.",
            { cause: error },
          );
        }

        const executionDeadline = Date.parse(started.executionDeadline);
        if (this.#now() >= executionDeadline) {
          return {
            outcome: "indeterminate",
            observed_before: observedBefore,
            actual_after: null,
            recalculation,
          };
        }

        // Office.js has no compare-and-set transaction. Re-read once after the network round trip
        // so a coauthor change during write-start publication is not knowingly overwritten.
        context.workbook.load("readOnly");
        worksheet.load(["id", "name", "enableCalculation"]);
        const prewriteHandles = prepareCellReads(worksheet, cells);
        await context.sync();
        assertWorksheet(
          worksheet,
          manifest.office_binding.sheet,
          manifest.office_binding.worksheet_id,
        );
        const prewriteState = materializeCellStates(worksheet, prewriteHandles);
        if (
          context.workbook.readOnly ||
          (manifest.diff.some((item) => item.kind === "set_formula") &&
            !worksheet.enableCalculation) ||
          !cellStatesEqual(prewriteState, manifest.before) ||
          this.#now() >= executionDeadline
        ) {
          return {
            outcome: "indeterminate",
            observed_before: observedBefore,
            actual_after: null,
            recalculation,
          };
        }

        applyChanges(worksheet, manifest.diff, this.#excel);
        await context.sync();

        if (manifest.diff.some((item) => item.kind === "set_formula")) {
          worksheet.calculate(true);
          recalculation = "recalculate";
          await context.sync();
        }

        const afterHandles = prepareCellReads(worksheet, cells);
        await context.sync();
        const actualAfter = materializeCellStates(worksheet, afterHandles);
        if (this.#now() >= executionDeadline) {
          return {
            outcome: "indeterminate",
            observed_before: observedBefore,
            actual_after: actualAfter,
            recalculation,
          };
        }
        return {
          outcome: "applied",
          observed_before: observedBefore,
          actual_after: actualAfter,
          recalculation,
        };
      });
    } catch (error) {
      if (
        error instanceof OfficeStartConfirmationError ||
        error instanceof OfficeStartRejectedError
      ) {
        throw error;
      }
      if (startConfirmed && observedBefore !== null) {
        return {
          outcome: "indeterminate",
          observed_before: observedBefore,
          actual_after: null,
          recalculation,
        };
      }
      throw new OfficeBeforeWriteError("Excel을 수정하기 전 셀 상태를 확정하지 못했습니다.", {
        cause: error,
      });
    }
  }

  async saveCurrentWorkbook(): Promise<void> {
    this.assertSupported();
    await this.#excelRun(async (context) => {
      context.workbook.save(this.#excel.SaveBehavior.save);
      await context.sync();
    });
  }
}

function prepareCellReads(worksheet: Excel.Worksheet, cells: string[]): CellReadHandle[] {
  worksheet.protection.load("protected");
  return cells.map((cell) => {
    const range = worksheet.getRange(cell);
    range.load(["values", "formulas", "numberFormat", "text", "valueTypes", "savedAsArray"]);
    range.format.protection.load("locked");
    const merged = range.getMergedAreasOrNullObject();
    merged.load("isNullObject");
    const spillParent = range.getSpillParentOrNullObject();
    spillParent.load(["isNullObject", "address"]);
    const spillingTo = range.getSpillingToRangeOrNullObject();
    spillingTo.load(["isNullObject", "address"]);
    const formulaCells = range.getSpecialCellsOrNullObject("Formulas");
    formulaCells.load("isNullObject");
    const tables = range.getTables(false);
    tables.load("items/id");
    return { cell, range, merged, spillParent, spillingTo, formulaCells, tables };
  });
}

function materializeCellStates(
  worksheet: Excel.Worksheet,
  handles: CellReadHandle[],
): CellState[] {
  return handles.map((handle) => {
    const rawValue = first(handle.range.values, "values", handle.cell);
    const rawFormula = first(handle.range.formulas, "formulas", handle.cell);
    const rawText = first(handle.range.text, "text", handle.cell);
    const rawType = first(handle.range.valueTypes, "valueTypes", handle.cell);
    const rawFormat = first(handle.range.numberFormat, "numberFormat", handle.cell);
    const calculated = calculatedValue(rawType, rawValue, rawText, handle.cell);
    return {
      cell: handle.cell,
      authored: authoredState(
        rawType,
        rawFormula,
        rawValue,
        !handle.formulaCells.isNullObject,
        handle.cell,
      ),
      calculated_value: calculated.value,
      calculated_type: calculated.type,
      number_format: requireString(rawFormat, "numberFormat", handle.cell, 500),
      target_constraints: {
        merged: !handle.merged.isNullObject,
        spill: spillState(handle),
        // V1 cannot express allow-edit ranges or paused protection. A protected sheet therefore
        // fails closed even when one cell happens to report locked=false.
        protected: worksheet.protection.protected,
        table_member: handle.tables.items.length > 0,
      },
    };
  });
}

function authoredState(
  rawType: Excel.RangeValueType,
  rawFormula: string | number | boolean,
  rawValue: string | number | boolean,
  hasFormula: boolean,
  cell: string,
): CellState["authored"] {
  if (hasFormula) {
    if (
      typeof rawFormula !== "string" ||
      !rawFormula.startsWith("=") ||
      rawFormula.length > 8192
    ) {
      throw new WorkbookEditContractError("UNSUPPORTED_CELL_VALUE", `${cell} formula가 너무 깁니다.`);
    }
    return { kind: "formula", formula: rawFormula };
  }
  if (rangeValueType(rawType) === "empty") {
    return { kind: "blank" };
  }
  if (rangeValueType(rawType) === "error") {
    throw new WorkbookEditContractError(
      "UNSUPPORTED_CELL_VALUE",
      `${cell} literal Excel error는 authored value로 안전하게 재현할 수 없습니다.`,
    );
  }
  const value = requireScalar(rawValue, cell);
  return { kind: "value", value };
}

function calculatedValue(
  rawType: Excel.RangeValueType,
  rawValue: string | number | boolean,
  rawText: string,
  cell: string,
): {
  type: CellState["calculated_type"];
  value: CellState["calculated_value"];
} {
  const type = rangeValueType(rawType);
  if (type === "empty") {
    return { type, value: null };
  }
  if (type === "error") {
    const value = requireString(rawValue, "error value", cell, 32767).startsWith("#")
      ? String(rawValue)
      : requireString(rawText, "error text", cell, 32767);
    if (!value.startsWith("#")) {
      throw new WorkbookEditContractError(
        "UNSUPPORTED_CELL_VALUE",
        `${cell} Excel error를 canonical 문자열로 읽을 수 없습니다.`,
      );
    }
    return { type, value };
  }
  const value = requireScalar(rawValue, cell);
  if (typeof value !== type) {
    throw new WorkbookEditContractError(
      "UNSUPPORTED_CELL_VALUE",
      `${cell} value type과 계산값이 일치하지 않습니다.`,
    );
  }
  return { type, value };
}

function rangeValueType(value: Excel.RangeValueType): CellState["calculated_type"] {
  const normalized = String(value).toLowerCase();
  if (normalized === "empty") return "empty";
  if (normalized === "string") return "string";
  if (normalized === "double" || normalized === "integer") return "number";
  if (normalized === "boolean") return "boolean";
  if (normalized === "error") return "error";
  throw new WorkbookEditContractError(
    "UNSUPPORTED_CELL_VALUE",
    `지원하지 않는 Excel value type입니다: ${normalized}`,
  );
}

function spillState(handle: CellReadHandle): SpillState {
  // ExcelApi 1.12 exposes legacy/CSE and compatibility array membership separately from
  // dynamic spill ranges. V1 has one array/spill safety flag, so classify either as unsafe.
  if (handle.range.savedAsArray) {
    return "parent";
  }
  if (!handle.spillingTo.isNullObject) {
    return "parent";
  }
  if (!handle.spillParent.isNullObject) {
    return "child";
  }
  return "none";
}

function applyChanges(
  worksheet: Excel.Worksheet,
  changes: EditChange[],
  excel: typeof Excel,
): void {
  for (const change of changes) {
    const range = worksheet.getRange(change.cell);
    if (change.kind === "set_value") {
      if (change.after.authored.kind !== "value") {
        throw new WorkbookEditContractError("UNSAFE_MANIFEST", "set_value authored state가 다릅니다.");
      }
      range.values = [[change.after.authored.value]];
    } else if (change.kind === "set_formula") {
      if (change.after.authored.kind !== "formula") {
        throw new WorkbookEditContractError("UNSAFE_MANIFEST", "set_formula authored state가 다릅니다.");
      }
      range.formulas = [[change.after.authored.formula]];
    } else if (change.kind === "set_number_format") {
      range.numberFormat = [[change.after.number_format]];
    } else if (change.kind === "clear_contents") {
      range.clear(excel.ClearApplyTo.contents);
    } else {
      const exhaustive: never = change.kind;
      throw new WorkbookEditContractError("UNSAFE_MANIFEST", `알 수 없는 edit kind: ${exhaustive}`);
    }
  }
}

function assertWorksheet(worksheet: Excel.Worksheet, sheet: string, worksheetId: string): void {
  if (worksheet.name !== sheet || worksheet.id !== worksheetId) {
    throw new WorkbookEditContractError(
      "WORKSHEET_MISMATCH",
      "열린 Excel worksheet가 승인된 worksheet와 다릅니다.",
    );
  }
}

function cellStatesEqual(left: CellState[], right: CellState[]): boolean {
  if (left.length !== right.length) return false;
  return left.every((value, index) => strictJsonEqual(value, right[index]));
}

function strictJsonEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (
    typeof left !== "object" ||
    left === null ||
    typeof right !== "object" ||
    right === null
  ) {
    return false;
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => strictJsonEqual(value, right[index]))
    );
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord).sort();
  const rightKeys = Object.keys(rightRecord).sort();
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key, index) =>
        key === rightKeys[index] && strictJsonEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

function first<T>(matrix: T[][], field: string, cell: string): T {
  const value = matrix[0]?.[0];
  if (value === undefined) {
    throw new WorkbookEditContractError(
      "OFFICE_READBACK_MISMATCH",
      `${cell} ${field} readback이 single-cell shape가 아닙니다.`,
    );
  }
  return value;
}

function requireScalar(value: unknown, cell: string): Scalar {
  if (typeof value === "string" && value.length <= 32767) return value;
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) {
    return value;
  }
  throw new WorkbookEditContractError(
    "UNSUPPORTED_CELL_VALUE",
    `${cell} 값은 bounded JSON scalar가 아닙니다.`,
  );
}

function requireString(value: unknown, field: string, cell: string, maximum: number): string {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new WorkbookEditContractError(
      "OFFICE_READBACK_MISMATCH",
      `${cell} ${field} 문자열이 유효하지 않습니다.`,
    );
  }
  return value;
}
