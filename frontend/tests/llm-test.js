// vLLM 多轮对话客户端（Node + openai SDK）
// 用法: node chat.mjs     （输入内容回车，exit/quit 退出）
import OpenAI from "openai";
import readline from "node:readline";

const client = new OpenAI({
  baseURL: process.env.VLLM_BASE ?? "http://172.16.103.222:8000/v1",
  apiKey: process.env.VLLM_KEY ?? "sk-vllm-demo-2026",
});

const MODEL = "qwen3-14b";
const MAX_TOKENS = 512;      // 单轮回复上限
const CHAR_BUDGET = 20000;   // 历史字符预算（中文≈1.6 字/token，约 12.5k token）

const messages = [{ role: "system", content: "你是简洁的中文助手。" }];

// 保留 system + 最近的整轮；从最早的一轮开始丢
function trim(msgs, maxChars = CHAR_BUDGET) {
  const head = msgs.filter((m) => m.role === "system");
  const rest = msgs.filter((m) => m.role !== "system");
  const size = (arr) => arr.reduce((n, m) => n + (m.content?.length ?? 0), 0);
  // 至少保留最后一条（本次提问），超长让它交给服务端判 400，而不是悄悄丢掉
  while (rest.length > 1 && size(head) + size(rest) > maxChars) {
    rest.shift();
    while (rest.length > 1 && rest[0].role !== "user") rest.shift();
  }
  return [...head, ...rest];
}

async function chatOnce(text) {
  messages.push({ role: "user", content: text });   // 先入历史
  try {
    const stream = await client.chat.completions.create({
      model: MODEL,
      messages: trim(messages),                     // 发的是裁剪后的副本
      stream: true,
      max_tokens: MAX_TOKENS,
      temperature: 0.7,
      chat_template_kwargs: { enable_thinking: false },  // 顶层直接写，Node SDK 会透传
    });

    const parts = [];
    for await (const chunk of stream) {
      const piece = chunk.choices?.[0]?.delta?.content;
      if (piece) { parts.push(piece); process.stdout.write(piece); }
    }
    process.stdout.write("\n");

    const answer = parts.join("");
    messages.push({ role: "assistant", content: answer });
    return answer;
  } catch (err) {
    messages.pop();                                  // 失败回滚，否则一次超限整段会话报废
    throw err;
  }
}

const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
console.log("💡 输入问题回车，exit 退出。\n");
for await (const line of rl) {
  const text = line.trim();
  if (text === "exit" || text === "quit") break;
  if (!text) continue;
  try {
    await chatOnce(text);
  } catch (err) {
    console.error(`❌ 请求失败：${err.message}（已回滚该轮，可继续输入）`);
  }
  process.stdout.write("\n");
}
rl.close();