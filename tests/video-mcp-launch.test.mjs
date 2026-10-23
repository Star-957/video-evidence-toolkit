import test from "node:test";
import assert from "node:assert/strict";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const url = new URL("../video-mcp-launch.mjs", import.meta.url);
test("launch configuration uses the installed server and sanitizes a copy of the environment", async () => {
  assert.ok(existsSync(url), "shared installed launcher must exist");
  const { createVideoMcpLaunch } = await import(url);
  const input = { KEEP: "yes", OPENAI_API_KEY: "fixture", openai_admin_key: "fixture", CONTROL_PLANE_API_KEY: "fixture", MCP_WRITE_SIDECARS: "0", ignored: undefined };
  const launch = createVideoMcpLaunch(input);
  assert.equal(launch.command, process.execPath);
  assert.deepEqual(launch.args, [fileURLToPath(new URL("../node_modules/mcp-video-analyzer/dist/index.js", import.meta.url))]);
  assert.deepEqual(launch.env, { KEEP: "yes", MCP_WRITE_SIDECARS: "1" });
  assert.equal(input.OPENAI_API_KEY, "fixture");
  assert.equal(launch.cwd, fileURLToPath(new URL("../", import.meta.url)));
});

test("both shell wrappers and the opt-in MCP client use the shared definition", () => {
  for (const name of ["run-video-mcp.cmd", "run-video-mcp.ps1"]) {
    const source = readFileSync(new URL(`../${name}`, import.meta.url), "utf8");
    assert.match(source, /video-mcp-launch\.mjs/);
    assert.doesNotMatch(source, /npx|@latest|node_modules/i);
  }
  const smoke = readFileSync(new URL("../test-video-mcp.mjs", import.meta.url), "utf8");
  assert.match(smoke, /createVideoMcpLaunch/);
  assert.doesNotMatch(smoke, /node_modules|OPENAI_API_KEY/);
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.scripts.test, "node --test tests/*.test.mjs");
  assert.equal(pkg.scripts["test:mcp"], "node test-video-mcp.mjs");
  assert.match(pkg.scripts["test:python"], /unittest discover/);
});

test("shared launcher propagates actual child failure without launching MCP", async () => {
  assert.ok(existsSync(url), "shared installed launcher must exist");
  const { runVideoMcp } = await import(url);
  const temporaryParent = resolve(tmpdir());
  const root = mkdtempSync(join(temporaryParent, "video-child-contract-"));
  try {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
      /^(path|pathext|systemroot|systemdrive|comspec|windir)$/i.test(key)));
    Object.assign(env, { TEMP: root, TMP: root });
    assert.equal(await runVideoMcp({ command: process.execPath, args: ["-e", "process.exit(7)"], cwd: root, env }), 7);
    const missingCommand = join(root, "not-a-command.exe");
    assert.equal(existsSync(missingCommand), false);
    await assert.rejects(runVideoMcp({ command: missingCommand, args: [], cwd: root, env }), /ENOENT/);
  } finally {
    const bounded = relative(temporaryParent, resolve(root));
    assert(bounded.startsWith("video-child-contract-") && !bounded.includes(sep));
    rmSync(root, { recursive: true, force: true });
  }
});

test("Windows PowerShell wrapper preserves complete arguments and child failure", { skip: process.platform !== "win32" }, () => {
  const temporaryParent = resolve(tmpdir());
  const root = mkdtempSync(join(temporaryParent, "video-launch-contract-"));
  const fixture = join(root, "fixture with spaces");
  try {
    mkdirSync(join(fixture, "node_modules/mcp-video-analyzer/dist"), { recursive: true });
    for (const name of ["video-mcp-launch.mjs", "run-video-mcp.ps1"]) {
      copyFileSync(new URL(`../${name}`, import.meta.url), join(fixture, name));
    }
    writeFileSync(join(fixture, "node_modules/mcp-video-analyzer/dist/index.js"), `
      console.log(JSON.stringify({ args: process.argv.slice(2), cwd: process.cwd(),
        keep: process.env.AUDIT_KEEP, sidecars: process.env.MCP_WRITE_SIDECARS,
        blocked: Object.keys(process.env).filter(key => /^(CONTROL_PLANE_API_KEY|OPENAI_API_KEY|OPENAI_ADMIN_KEY)$/i.test(key)) }));
      process.exit(7);
    `, { flag: "wx" });
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
      /^(path|pathext|systemroot|systemdrive|comspec|windir)$/i.test(key)));
    Object.assign(env, { TEMP: root, TMP: root, AUDIT_KEEP: "synthetic-only", MCP_WRITE_SIDECARS: "0",
      CONTROL_PLANE_API_KEY: "fixture", openai_api_key: "fixture", OpenAi_Admin_Key: "fixture" });
    const args = ["plain", "two words", "--label=space value", "東京", "", 'say "hello"', "C:\\path with spaces\\", 'backslash\\"quote'];
    // Decode values inside PowerShell so its own -File argument parser cannot mask a wrapper bug.
    const encodedArgs = Buffer.from(JSON.stringify(args), "utf8").toString("base64");
    const wrapperPath = join(fixture, "run-video-mcp.ps1").replaceAll("'", "''");
    const command = `$values = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encodedArgs}'))); & '${wrapperPath}' @values; exit $LASTEXITCODE`;
    const child = spawnSync(join(process.env.SystemRoot, "System32/WindowsPowerShell/v1.0/powershell.exe"),
      ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(command, "utf16le").toString("base64")],
      { cwd: temporaryParent, env, encoding: "utf8", timeout: 10_000, maxBuffer: 1024 * 1024, windowsHide: true });
    assert.ifError(child.error);
    assert.equal(child.status, 7, child.stderr);
    const output = JSON.parse(child.stdout.trim());
    assert.deepEqual(output.args, args);
    assert.equal(output.cwd, fixture);
    assert.equal(output.keep, "synthetic-only");
    assert.equal(output.sidecars, "1");
    assert.deepEqual(output.blocked, []);
  } finally {
    const bounded = relative(temporaryParent, resolve(root));
    assert(bounded.startsWith("video-launch-contract-") && !bounded.includes(sep));
    rmSync(root, { recursive: true, force: true });
  }
});
