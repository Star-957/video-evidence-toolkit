import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

export function createVideoMcpLaunch(environment = process.env) {
  const blocked = new Set(["CONTROL_PLANE_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY"]);
  const env = Object.fromEntries(Object.entries(environment).filter(
    ([key, value]) => typeof value === "string" && !blocked.has(key.toUpperCase()),
  ));
  env.MCP_WRITE_SIDECARS = "1";
  return {
    command: process.execPath,
    args: [fileURLToPath(new URL("./node_modules/mcp-video-analyzer/dist/index.js", import.meta.url))],
    cwd: fileURLToPath(new URL("./", import.meta.url)),
    env,
  };
}

export function runVideoMcp({ command, args, cwd, env } = createVideoMcpLaunch()) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: "inherit", windowsHide: true });
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? 1));
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const launch = createVideoMcpLaunch();
  launch.args.push(...process.argv.slice(2));
  try {
    process.exitCode = await runVideoMcp(launch);
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
