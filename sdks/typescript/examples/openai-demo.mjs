/**
 * Live end-to-end demo of the Memanto <-> OpenAI SDK integration.
 *
 * It boots a local Memanto server, exposes the three memory tools to the
 * OpenAI `runTools()` helper, and lets the model remember + recall a fact
 * across two turns.
 *
 * Prerequisites:
 *   - OPENAI_API_KEY set in the environment
 *   - Memanto credentials configured (or an on-prem backend) so the spawned
 *     server can reach Moorcheh
 *   - `npm run build` has been run in sdks/typescript (this imports ./dist)
 *
 * Run:
 *   node examples/openai-demo.mjs
 */
import OpenAI from "openai";

import { Memanto } from "../dist/index.js";
import { createMemantoOpenAITools } from "../dist/integrations/openai.js";

const MODEL = process.env.OPENAI_MODEL ?? "gpt-4o-mini";

async function main() {
  if (!process.env.OPENAI_API_KEY) {
    throw new Error("Set OPENAI_API_KEY to run this demo.");
  }

  const client = new OpenAI();
  const memanto = new Memanto({ agentId: `openai-demo-${Date.now()}` });

  try {
    const tools = createMemantoOpenAITools(memanto, { defaultLimit: 5 });

    console.log("\n--- Turn 1: teach + store a preference ---");
    const store = client.chat.completions.runTools({
      model: MODEL,
      tools,
      messages: [
        {
          role: "system",
          content:
            "You are a helpful assistant with long-term memory. " +
            "Persist durable user facts and preferences with rememberMemory.",
        },
        {
          role: "user",
          content: "Remember that Alex switched from oat milk to soy milk today.",
        },
      ],
    });
    console.log(await store.finalContent());

    console.log("\n--- Turn 2: recall it in a fresh conversation ---");
    const recall = client.chat.completions.runTools({
      model: MODEL,
      tools,
      messages: [
        {
          role: "system",
          content:
            "You are a helpful assistant with long-term memory. " +
            "Use recallMemory or answerMemory before answering questions " +
            "about the user's past.",
        },
        { role: "user", content: "What milk does Alex drink now?" },
      ],
    });
    console.log(await recall.finalContent());
  } finally {
    await memanto.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
