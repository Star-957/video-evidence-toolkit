import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { createVideoMcpLaunch } from "./video-mcp-launch.mjs";

const transport = new StdioClientTransport({
  ...createVideoMcpLaunch(),
  stderr: "inherit"
});

const client = new Client(
  {
    name: "neuro-video-test",
    version: "1.0.0"
  },
  {
    capabilities: {}
  }
);

try {
  await client.connect(transport);
  const result = await client.listTools();

  console.log("");
  console.log("===== MCP TOOLS OK =====");
  console.log(JSON.stringify(result, null, 2));

  await client.close();
} catch (error) {
  console.error("");
  console.error("===== MCP TEST FAILED =====");
  console.error(error);

  try {
    await client.close();
  } catch {}

  process.exit(1);
}
