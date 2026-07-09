import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import { basename, join, resolve, delimiter } from "node:path";

const MAX_TEXT = 24000;

function cleanPath(input: string): string {
  return input.startsWith("@") ? input.slice(1) : input;
}

function pyEnv(cwd: string): NodeJS.ProcessEnv {
  const src = join(cwd, "src");
  return {
    ...process.env,
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONPATH: process.env.PYTHONPATH ? `${src}${delimiter}${process.env.PYTHONPATH}` : src,
  };
}

function python(cwd?: string): string {
  if (process.env.BARNDSL_PYTHON) return process.env.BARNDSL_PYTHON;
  if (cwd) {
    const venv = process.platform === "win32" ? join(cwd, ".venv", "Scripts", "python.exe") : join(cwd, ".venv", "bin", "python");
    if (existsSync(venv)) return venv;
  }
  if (process.platform === "win32" && process.env.USERPROFILE) {
    const root = join(process.env.USERPROFILE, ".pyenv", "pyenv-win");
    const versionFile = join(root, "version");
    if (existsSync(versionFile)) {
      const version = readFileSync(versionFile, "utf8").trim().split(/\s+/)[0];
      const exe = join(root, "versions", version, "python.exe");
      if (existsSync(exe)) return exe;
    }
  }
  return "python";
}

function trim(text: string, max = MAX_TEXT): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + `\n… truncated ${text.length - max} chars`;
}

function tryJson(text: string): unknown | undefined {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function run(cwd: string, args: string[], signal?: AbortSignal, input?: string): Promise<{ code: number | null; stdout: string; stderr: string; command: string }> {
  return new Promise((resolvePromise, reject) => {
    const exe = python(cwd);
    const proc = spawn(exe, args, {
      cwd,
      env: pyEnv(cwd),
      stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");
    proc.stdout.on("data", (chunk) => (stdout += chunk));
    proc.stderr.on("data", (chunk) => (stderr += chunk));
    proc.on("error", reject);
    proc.on("close", (code) => resolvePromise({ code, stdout, stderr, command: `${exe} ${args.join(" ")}` }));
    if (input !== undefined && proc.stdin) {
      proc.stdin.write(input, "utf8");
      proc.stdin.end();
    }
    signal?.addEventListener("abort", () => proc.kill(), { once: true });
  });
}

function resultText(prefix: string, r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const parts = [`${prefix} (exit ${r.code ?? "signal"})`, `$ ${r.command}`];
  if (r.stdout.trim()) parts.push(`stdout:\n${trim(r.stdout)}`);
  if (r.stderr.trim()) parts.push(`stderr:\n${trim(r.stderr)}`);
  return parts.join("\n\n");
}

function summarizePytest(stdout: string, stderr: string): Record<string, unknown> {
  const text = `${stdout}\n${stderr}`;
  const failed = [...text.matchAll(/^FAILED\s+([^\s]+)\s+-\s+(.+)$/gm)].map((m) => ({ test: m[1], reason: m[2] }));
  const summary = text.match(/=+\s*(.*?)\s*=+\s*$/gm)?.slice(-1)[0] ?? "";
  return { failed, summary };
}

function formatAudit(r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const j = tryJson(r.stdout) as any;
  if (!j || typeof j !== "object") return resultText("barndsl repo audit", r);
  return [
    `barndsl repo audit (exit ${r.code ?? "signal"})`,
    `ok: ${j.ok}`,
    `problems: ${(j.problems ?? []).length ? (j.problems ?? []).join("; ") : "none"}`,
    `statements: parser ${j.statement_keywords?.parser_count}, playground ${j.statement_keywords?.playground_count}, lsp quickfix ${j.statement_keywords?.lsp_quickfix_count}`,
    `diagnostic registry: emitted ${j.diagnostics?.emitted_literal_issue_codes}, registered ${j.diagnostics?.registered_codes}, missing ${(j.diagnostics?.missing_registry ?? []).length}`,
    `$ ${r.command}`,
    r.stderr.trim() ? `stderr:\n${trim(r.stderr)}` : "",
  ].filter(Boolean).join("\n");
}

function formatDiagDiff(label: string, r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const j = tryJson(r.stdout) as any;
  if (!j || typeof j !== "object") return resultText(label, r);
  const top = Object.entries(j.after ?? {}).sort((a: any, b: any) => b[1] - a[1]).slice(0, 12).map(([k, v]) => `${k}:${v}`).join(", ") || "none";
  return [
    `${label} (exit ${r.code ?? "signal"})`,
    `files: ${j.after_files ?? "?"} (${j.after_whole_plans ?? "?"} plans, ${j.after_fragments ?? "?"} fragments)`,
    `score: min ${j.score?.min ?? "n/a"}, avg ${j.score?.avg ?? "n/a"}, max ${j.score?.max ?? "n/a"}`,
    `introduced: ${Object.keys(j.introduced ?? {}).length}, resolved: ${Object.keys(j.resolved ?? {}).length}, increased: ${Object.keys(j.increased ?? {}).length}, reduced: ${Object.keys(j.reduced ?? {}).length}`,
    `top codes: ${top}`,
    `$ ${r.command}`,
    r.stderr.trim() ? `stderr:\n${trim(r.stderr)}` : "",
  ].filter(Boolean).join("\n");
}

function formatDoctor(r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const j = tryJson(r.stdout) as any;
  if (!j || typeof j !== "object") return resultText("barndsl doctor", r);
  const checks = (j.checks ?? []).map((c: any) => `${c.ok ? "✓" : "✗"} ${c.name}`).join(", ");
  return [
    `barndsl doctor (exit ${r.code ?? "signal"})`,
    `ok: ${j.ok}`,
    `checks: ${checks}`,
    ...(j.next_steps ?? []).map((s: string) => `next: ${s}`),
    `$ ${r.command}`,
    r.stderr.trim() ? `stderr:\n${trim(r.stderr)}` : "",
  ].filter(Boolean).join("\n");
}

function formatExportParity(r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const j = tryJson(r.stdout) as any;
  if (!j || typeof j !== "object") return resultText("barndsl export parity", r);
  const results = (j.results ?? []).map((x: any) => `${x.kind}:${x.exit === 0 && x.exists ? "ok" : "fail"}${x.bytes ? `(${x.bytes}b)` : ""}`).join(", ");
  return [
    `barndsl export parity (exit ${r.code ?? "signal"})`,
    `ok: ${j.ok}`,
    `path: ${j.path}`,
    `artifacts: ${results}`,
    `$ ${r.command}`,
    r.stderr.trim() ? `stderr:\n${trim(r.stderr)}` : "",
  ].filter(Boolean).join("\n");
}

function formatLocate(r: { code: number | null; stdout: string; stderr: string; command: string }): string {
  const j = tryJson(r.stdout) as any;
  if (!j || typeof j !== "object") return resultText("barndsl locate", r);
  const exact = Object.entries(j.exact ?? {}).map(([k, v]: any) => `${k}:${v.path ?? v.registry?.path ?? v.compiler_keywords?.path ?? "see details"}`).join(", ") || "none";
  const counts = Object.entries(j.matches ?? {}).map(([k, v]: any) => `${k}:${Array.isArray(v) ? v.length : 0}`).join(", ");
  const firstHits = Object.entries(j.matches ?? {}).flatMap(([k, v]: any) => Array.isArray(v) ? v.slice(0, 3).map((x: any) => `${k} ${x.path}:${x.line} ${x.text}`) : []).slice(0, 8);
  return [
    `barndsl locate (exit ${r.code ?? "signal"})`,
    `ok: ${j.ok}`,
    `query: ${j.query}`,
    `kind: ${(j.kind ?? []).join(", ")}`,
    `exact: ${exact}`,
    `matches: ${counts}`,
    ...firstHits,
    `$ ${r.command}`,
    r.stderr.trim() ? `stderr:\n${trim(r.stderr)}` : "",
  ].filter(Boolean).join("\n");
}

function walkCodes(value: unknown, out: Map<string, number>): void {
  if (Array.isArray(value)) {
    for (const v of value) walkCodes(v, out);
    return;
  }
  if (!value || typeof value !== "object") return;
  const obj = value as Record<string, unknown>;
  const code = obj.code;
  if (typeof code === "string" && /^[A-Z][A-Z0-9_]+$/.test(code)) out.set(code, (out.get(code) ?? 0) + 1);
  for (const v of Object.values(obj)) walkCodes(v, out);
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "barndsl_compile",
    label: "barndsl compile",
    description: "Compile a .barn file with UTF-8-safe project defaults and return structured diagnostics when possible.",
    promptSnippet: "Compile barndsl plans with diagnostics, metrics, coordinates, and JSON output.",
    promptGuidelines: [
      "Use barndsl_compile instead of raw bash when checking whether a .barn plan compiles.",
      "When fixing .barn files, run barndsl_compile after edits and use the returned diagnostic codes and hints.",
    ],
    parameters: Type.Object({
      path: Type.String({ description: "Path to the .barn file" }),
      json: Type.Optional(Type.Boolean({ default: true })),
      showCoords: Type.Optional(Type.Boolean({ default: false })),
      metrics: Type.Optional(Type.Boolean({ default: false })),
      strict: Type.Optional(Type.Boolean({ default: false })),
      strictInfo: Type.Optional(Type.Boolean({ default: false })),
      profile: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["-m", "barndsl.cli", "compile", cleanPath(params.path)];
      if (params.json ?? true) args.push("--json");
      if (params.showCoords) args.push("--show-coords");
      if (params.metrics) args.push("--metrics");
      if (params.strict) args.push("--strict");
      if (params.strictInfo) args.push("--strict-info");
      if (params.profile) args.push("--profile", params.profile);
      const r = await run(ctx.cwd, args, signal);
      return { content: [{ type: "text", text: resultText("barndsl compile", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_inspect",
    label: "barndsl inspect",
    description: "Dump resolved barndsl geometry: rooms, exterior walls, adjacency, unplaced pockets, and free wall spans.",
    promptSnippet: "Inspect resolved barndsl geometry and legal opening spans.",
    promptGuidelines: [
      "Use barndsl_inspect when a barndsl fix needs spatial facts such as room coordinates, exterior walls, adjacency, or free wall spans.",
    ],
    parameters: Type.Object({
      path: Type.String(),
      json: Type.Optional(Type.Boolean({ default: true })),
      room: Type.Optional(Type.String({ description: "Optional room id filter" })),
      freeSpansOnly: Type.Optional(Type.Boolean({ default: false })),
      adjacencyOnly: Type.Optional(Type.Boolean({ default: false })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["-m", "barndsl.cli", "inspect", cleanPath(params.path)];
      if (params.json ?? true) args.push("--json");
      const r = await run(ctx.cwd, args, signal);
      let parsed = tryJson(r.stdout) as any;
      if (parsed && typeof parsed === "object") {
        if (params.room) {
          parsed = { ...parsed,
            rooms: (parsed.rooms ?? []).filter((x: any) => x.id === params.room),
            adjacency: (parsed.adjacency ?? []).filter((x: any) => x.a === params.room || x.b === params.room),
            free_spans: (parsed.free_spans ?? []).filter((x: any) => x.room === params.room),
          };
        }
        if (params.freeSpansOnly) parsed = { free_spans: parsed.free_spans ?? [] };
        if (params.adjacencyOnly) parsed = { adjacency: parsed.adjacency ?? [] };
      }
      const text = parsed ? JSON.stringify(parsed, null, 2) : resultText("barndsl inspect", r);
      return { content: [{ type: "text", text: trim(text) }], details: { ...r, json: parsed } };
    },
  });

  pi.registerTool({
    name: "barndsl_explain",
    label: "barndsl explain",
    description: "Explain a barndsl diagnostic code using the project's registry.",
    promptSnippet: "Explain barndsl diagnostic codes.",
    parameters: Type.Object({ code: Type.Optional(Type.String({ description: "Diagnostic code such as BEDROOM_EGRESS; omit to list all" })) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["-m", "barndsl.cli", "explain"];
      if (params.code) args.push(params.code);
      const r = await run(ctx.cwd, args, signal);
      return { content: [{ type: "text", text: resultText("barndsl explain", r) }], details: r };
    },
  });

  pi.registerTool({
    name: "barndsl_render_preview",
    label: "barndsl render preview",
    description: "Build a barndsl drawing into .pi/artifacts and return the artifact path and command output.",
    promptSnippet: "Render barndsl plans to SVG/PNG/PDF preview artifacts.",
    promptGuidelines: ["Use barndsl_render_preview when a visual floor-plan check would help validate a .barn change."],
    parameters: Type.Object({
      path: Type.String(),
      out: Type.Optional(Type.String({ description: "Output path; defaults under .pi/artifacts" })),
      format: Type.Optional(StringEnum(["svg", "png", "pdf"] as const)),
      dims: Type.Optional(StringEnum(["nominal", "faces"] as const)),
      frame: Type.Optional(Type.Boolean({ default: false })),
      profile: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      await mkdir(join(ctx.cwd, ".pi", "artifacts"), { recursive: true });
      const fmt = params.format ?? "svg";
      const defaultOut = join(".pi", "artifacts", `${basename(cleanPath(params.path)).replace(/\.[^.]+$/, "")}.${fmt}`);
      const out = params.out ? cleanPath(params.out) : defaultOut;
      const args = ["-m", "barndsl.cli", "build", cleanPath(params.path), "--out", out, "--format", fmt];
      if (params.dims) args.push("--dims", params.dims);
      if (params.frame) args.push("--frame");
      if (params.profile) args.push("--profile", params.profile);
      const r = await run(ctx.cwd, args, signal);
      return { content: [{ type: "text", text: `${resultText("barndsl render", r)}\n\nartifact: ${out}` }], details: { ...r, out } };
    },
  });

  pi.registerTool({
    name: "barndsl_test",
    label: "barndsl pytest",
    description: "Run pytest with this repo's UTF-8/PYTHONPATH defaults and summarize failures.",
    promptSnippet: "Run repo tests with UTF-8-safe defaults.",
    promptGuidelines: ["Use barndsl_test instead of raw pytest so Windows UTF-8 and PYTHONPATH are set correctly."],
    parameters: Type.Object({
      targets: Type.Optional(Type.Array(Type.String(), { description: "pytest targets; omit for tests/test_compiler.py" })),
      full: Type.Optional(Type.Boolean({ default: false, description: "Run the full suite" })),
      quiet: Type.Optional(Type.Boolean({ default: true })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const targets = params.full ? [] : (params.targets && params.targets.length ? params.targets : ["tests/test_compiler.py"]);
      const args = ["-m", "pytest", ...(params.quiet ?? true ? ["-q"] : []), ...targets.map(cleanPath)];
      const r = await run(ctx.cwd, args, signal);
      return { content: [{ type: "text", text: resultText("pytest", r) }], details: { ...r, summary: summarizePytest(r.stdout, r.stderr) } };
    },
  });

  pi.registerTool({
    name: "barndsl_trace_analyze",
    label: "barndsl trace analyze",
    description: "Summarize an agent-run JSONL trace: event count, scores, diagnostic codes, and stop reasons when present.",
    promptSnippet: "Analyze barndsl agent JSONL traces.",
    parameters: Type.Object({ path: Type.String() }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const abs = resolve(ctx.cwd, cleanPath(params.path));
      const text = await readFile(abs, "utf8");
      const lines = text.split(/\r?\n/).filter((l) => l.trim());
      const codes = new Map<string, number>();
      const scores: number[] = [];
      const stopReasons = new Map<string, number>();
      let parsed = 0;
      for (const line of lines) {
        const obj = tryJson(line);
        if (obj === undefined) continue;
        parsed++;
        walkCodes(obj, codes);
        const raw = JSON.stringify(obj);
        for (const m of raw.matchAll(/"(?:score|total)"\s*:\s*([0-9]+(?:\.[0-9]+)?)/g)) scores.push(Number(m[1]));
        for (const m of raw.matchAll(/"stop_reason"\s*:\s*"([^"]+)"/g)) stopReasons.set(m[1], (stopReasons.get(m[1]) ?? 0) + 1);
      }
      const summary = {
        path: cleanPath(params.path),
        lines: lines.length,
        parsed,
        scores,
        bestScore: scores.length ? Math.max(...scores) : null,
        diagnosticCodes: Object.fromEntries([...codes.entries()].sort()),
        stopReasons: Object.fromEntries([...stopReasons.entries()].sort()),
      };
      return { content: [{ type: "text", text: JSON.stringify(summary, null, 2) }], details: summary };
    },
  });

  pi.registerTool({
    name: "barndsl_edit",
    label: "barndsl DSL edit",
    description: "Apply a structured DSL-aware source edit using barndsl.edits, preserving comments and formatting where possible.",
    promptSnippet: "Apply structured barndsl source edits such as move_room, resize_room, move_opening, add_room, add_opening, etc.",
    promptGuidelines: [
      "Use barndsl_edit for mechanical .barn source changes when the edit maps to barndsl.edits; use normal edit for broader refactors.",
      "After barndsl_edit changes a plan, run barndsl_compile to verify diagnostics.",
    ],
    parameters: Type.Object({
      path: Type.String(),
      edit: Type.Record(Type.String(), Type.Any(), { description: "A barndsl.edits.Edit-shaped object, e.g. {kind:'move_room', room:'kitchen', x:24, y:0}" }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const abs = resolve(ctx.cwd, cleanPath(params.path));
      return withFileMutationQueue(abs, async () => {
        const payload = JSON.stringify({ path: cleanPath(params.path), edit: params.edit });
        const r = await run(ctx.cwd, ["tools/pi_barndsl_edit.py"], signal, payload);
        const parsed = tryJson(r.stdout);
        return { content: [{ type: "text", text: resultText("barndsl edit", r) }], details: { ...r, json: parsed } };
      });
    },
  });

  pi.registerTool({
    name: "barndsl_repo_audit",
    label: "barndsl repo audit",
    description: "Audit barndsl rule/feature wiring drift: diagnostic registry coverage, statement keyword surfaces, docs references, and artifact hygiene.",
    promptSnippet: "Audit barndsl repository wiring before/after adding DSL features or validation rules.",
    promptGuidelines: ["Use barndsl_repo_audit after adding a diagnostic rule or DSL statement to catch missed registry/editor/docs wiring."],
    parameters: Type.Object({}),
    async execute(_id, _params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_repo_audit.py"], signal);
      return { content: [{ type: "text", text: formatAudit(r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_rule_probe",
    label: "barndsl rule probe",
    description: "Compile inline .barn source and assert expected/absent diagnostic codes; useful while developing a new rule.",
    promptSnippet: "Probe validation rules with inline source and expected diagnostic codes.",
    promptGuidelines: ["Use barndsl_rule_probe for quick red/green checks while adding or changing barndsl validation rules."],
    parameters: Type.Object({
      source: Type.String(),
      expect: Type.Optional(Type.Record(Type.String(), Type.Any(), { description: "e.g. {warning:['NAT_LIGHT'], absent:['BEDROOM_EGRESS']}" })),
      name: Type.Optional(Type.String()),
      profile: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_rule_probe.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: resultText("barndsl rule probe", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_diagnostic_diff",
    label: "barndsl diagnostic diff",
    description: "Compile two plan sets and report diagnostic-code deltas (introduced, resolved, increased, reduced).",
    promptSnippet: "Diff diagnostic codes across plan sets for rule-change impact analysis.",
    promptGuidelines: ["Use barndsl_diagnostic_diff to see how a new/changed rule affects examples or fixture plans."],
    parameters: Type.Object({
      before: Type.Array(Type.String(), { description: "Files, directories, or globs for baseline" }),
      after: Type.Optional(Type.Array(Type.String(), { description: "Files, directories, or globs for comparison; defaults to before" })),
      profile: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_diag_diff.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: formatDiagDiff("barndsl diagnostic diff", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_gallery_gate",
    label: "barndsl gallery gate",
    description: "Compile all .barn files in examples/ (or provided paths) and summarize diagnostics/scores.",
    promptSnippet: "Compile the gallery/examples as a lightweight integration gate.",
    parameters: Type.Object({ paths: Type.Optional(Type.Array(Type.String(), { description: "Defaults to ['examples']" })) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const payload = { before: params.paths && params.paths.length ? params.paths : ["examples"] };
      const r = await run(ctx.cwd, ["tools/pi_barndsl_diag_diff.py"], signal, JSON.stringify(payload));
      return { content: [{ type: "text", text: formatDiagDiff("barndsl gallery gate", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_doctor",
    label: "barndsl doctor",
    description: "Run the default maintainability gate: repo audit, gallery scan, strict LSP smoke, impact targets/tests, and optional export parity.",
    promptSnippet: "Run the barndsl developer doctor gate before/after substantial feature work.",
    parameters: Type.Object({
      paths: Type.Optional(Type.Array(Type.String(), { description: "Gallery/example paths to scan; defaults to ['examples']" })),
      lspStrict: Type.Optional(Type.Boolean({ default: true })),
      runImpact: Type.Optional(Type.Boolean({ default: false })),
      exportPlan: Type.Optional(Type.String({ description: "Optional .barn plan for export parity" })),
      profile: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_doctor.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: formatDoctor(r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_feature_check",
    label: "barndsl feature check",
    description: "Verify that a DSL feature/statement is wired across parser, docs, playground, LSP, and tests.",
    promptSnippet: "Check feature wiring drift after adding or extending DSL functionality.",
    parameters: Type.Object({
      name: Type.String({ description: "Feature or statement name" }),
      statement: Type.Optional(Type.Boolean({ default: true, description: "Whether this should be a parser statement keyword" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_feature_check.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: resultText("barndsl feature check", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_locate",
    label: "barndsl locate",
    description: "Find implementation, docs, tests, and example breadcrumbs for a diagnostic code, DSL statement, command, module, or feature.",
    promptSnippet: "Locate barndsl codebase breadcrumbs before editing unfamiliar diagnostics, DSL statements, or features.",
    promptGuidelines: [
      "Use barndsl_locate before changing unfamiliar validation rules or DSL features to find emitters, registry entries, tests, and docs.",
    ],
    parameters: Type.Object({
      query: Type.String({ description: "Diagnostic code, DSL statement, CLI command, module, or search term" }),
      maxResults: Type.Optional(Type.Number({ default: 80, description: "Maximum source hits per category" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_locate.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: formatLocate(r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_export_parity",
    label: "barndsl export parity",
    description: "Smoke-test that a plan compiles and builds key artifacts (SVG, glTF, IFC, packet when supported by CLI).",
    promptSnippet: "Check export/build parity after adding geometry-affecting DSL features.",
    parameters: Type.Object({ path: Type.String(), prefix: Type.Optional(Type.String({ description: "Output prefix under .pi/artifacts" })) }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["-m", "barndsl.cli", "dev", "export-parity", cleanPath(params.path)];
      if (params.prefix) args.push("--prefix", params.prefix);
      const r = await run(ctx.cwd, args, signal);
      return { content: [{ type: "text", text: formatExportParity(r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_lsp_smoke",
    label: "barndsl LSP smoke",
    description: "Smoke-test pure barndsl LSP helpers: diagnostics, hover, completions, formatting, code actions, symbols, and composed-id behavior.",
    promptSnippet: "Smoke-test barndsl LSP features after editor/composition changes.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Optional composed .barn fixture" })),
      strictComposed: Type.Optional(Type.Boolean({ default: false, description: "Fail the tool if composed/stamped-id checks fail" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_lsp_smoke.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: resultText("barndsl lsp smoke", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_impact_tests",
    label: "barndsl impact tests",
    description: "Map changed files to likely pytest targets and optionally run them with UTF-8/PYTHONPATH defaults.",
    promptSnippet: "Choose targeted barndsl tests from changed files.",
    parameters: Type.Object({
      changed: Type.Optional(Type.Array(Type.String(), { description: "Changed files; defaults to git status" })),
      run: Type.Optional(Type.Boolean({ default: false })),
      quiet: Type.Optional(Type.Boolean({ default: true })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_impact_tests.py"], signal, JSON.stringify(params));
      return { content: [{ type: "text", text: resultText("barndsl impact tests", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_rule_scaffold",
    label: "barndsl rule scaffold",
    description: "Generate a checklist and optional test skeleton for adding a new validation diagnostic rule.",
    promptSnippet: "Scaffold new barndsl validation rules with registry/test/checklist guidance.",
    parameters: Type.Object({
      code: Type.String({ description: "Diagnostic code, e.g. ROOM_HABITABLE" }),
      severity: Type.Optional(StringEnum(["error", "warning", "info"] as const)),
      testFile: Type.Optional(Type.String()),
      write: Type.Optional(Type.Boolean({ default: false, description: "Write the test skeleton if it does not exist" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_scaffold.py"], signal, JSON.stringify({ kind: "rule", ...params }));
      return { content: [{ type: "text", text: resultText("barndsl rule scaffold", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerTool({
    name: "barndsl_feature_scaffold",
    label: "barndsl feature scaffold",
    description: "Generate a checklist artifact for adding a new DSL statement or model feature across compiler, docs, editor, exports, and tests.",
    promptSnippet: "Scaffold/checklist new barndsl DSL features across all integration surfaces.",
    parameters: Type.Object({
      name: Type.String({ description: "Feature or statement name" }),
      out: Type.Optional(Type.String({ description: "Markdown checklist output path" })),
      write: Type.Optional(Type.Boolean({ default: true })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const r = await run(ctx.cwd, ["tools/pi_barndsl_scaffold.py"], signal, JSON.stringify({ kind: "feature", ...params }));
      return { content: [{ type: "text", text: resultText("barndsl feature scaffold", r) }], details: { ...r, json: tryJson(r.stdout) } };
    },
  });

  pi.registerCommand("barndsl-status", {
    description: "Show quick repo/harness status for Architecture-DSL",
    handler: async (_args, ctx) => {
      const r = await run(ctx.cwd, ["-m", "barndsl.cli", "--help"]);
      ctx.ui.notify(r.code === 0 ? "barndsl harness extension loaded" : "barndsl CLI check failed", r.code === 0 ? "info" : "error");
    },
  });
}
