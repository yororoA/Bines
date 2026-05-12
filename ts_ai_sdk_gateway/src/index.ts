import fs from 'node:fs';
import path from 'node:path';
import dotenv from 'dotenv';
import express from 'express';
import cors from 'cors';
import { generateText, generateObject, streamText } from 'ai';
import { z } from 'zod';
import { createDeepSeek } from '@ai-sdk/deepseek';

declare const process: any;

function loadGatewayEnv() {
  const candidates = ['.env.local', '.env', '.env.example'];
  for (const filename of candidates) {
    const fullPath = path.resolve(process.cwd(), filename);
    if (!fs.existsSync(fullPath)) continue;
    dotenv.config({ path: fullPath });
    console.log(`[TS AI Gateway] loaded env from ${filename}`);
    return;
  }
  console.warn('[TS AI Gateway] no env file found (.env.local/.env/.env.example)');
}

loadGatewayEnv();

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

const port = Number(process.env.PORT || 3100);

type Role = 'main' | 'tool' | 'summary' | 'bored';
type ToolChoiceMode = 'filter' | 'main';

type RoleConfig = {
  apiKey: string;
  model: string;
};

const roleConfigs: Record<Role, RoleConfig> = {
  main: {
    apiKey: process.env.DEEPSEEK_API_KEY_MAIN || process.env.DEEPSEEK_API_KEY || '',
    model: process.env.DEEPSEEK_MODEL_MAIN || process.env.DEEPSEEK_MODEL || 'deepseek-chat',
  },
  tool: {
    apiKey: process.env.DEEPSEEK_API_KEY_TOOL || process.env.DEEPSEEK_API_KEY || '',
    model: process.env.DEEPSEEK_MODEL_TOOL || process.env.DEEPSEEK_MODEL || 'deepseek-chat',
  },
  summary: {
    apiKey: process.env.DEEPSEEK_API_KEY_SUMMARY || process.env.DEEPSEEK_API_KEY || '',
    model: process.env.DEEPSEEK_MODEL_SUMMARY || process.env.DEEPSEEK_MODEL || 'deepseek-chat',
  },
  bored: {
    apiKey: process.env.DEEPSEEK_API_KEY_BORED || process.env.DEEPSEEK_API_KEY || '',
    model: process.env.DEEPSEEK_MODEL_BORED || process.env.DEEPSEEK_MODEL || 'deepseek-chat',
  },
};

for (const role of Object.keys(roleConfigs) as Role[]) {
  if (!roleConfigs[role].apiKey) {
    console.warn(`[TS AI Gateway] ${role} role api key is empty`);
  }
}

function resolveRole(role: string | undefined): Role {
  if (role === 'tool' || role === 'summary' || role === 'bored') {
    return role;
  }
  return 'main';
}

function normalizeMessages(messages: any[]): Array<{ role: 'system' | 'user' | 'assistant'; content: string }> {
  return messages
    .map((m: any) => ({
      role: m?.role,
      content: typeof m?.content === 'string' ? m.content : '',
    }))
    .filter((m: any) => ['system', 'user', 'assistant'].includes(m.role));
}

function normalizeAllowedTools(raw: any): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item !== 'string') continue;
    const name = item.trim();
    if (!name || out.includes(name)) continue;
    out.push(name);
  }
  return out;
}

function normalizeToolChoice(raw: any, allowedTools: string[]): string[] {
  if (!Array.isArray(raw)) return [];
  const allowed = new Set(allowedTools);
  const out: string[] = [];
  for (const item of raw) {
    if (typeof item !== 'string') continue;
    const name = item.trim();
    if (!name || !allowed.has(name) || out.includes(name)) continue;
    out.push(name);
  }
  return out;
}

function buildToolChoicePrompt(contextStr: string, mainOutput: string, allowedTools: string[]): string {
  return `You are a tool selector.
Given the dialogue context and assistant output, decide which tools should run.

Rules:
- Only choose from allowed list: ${allowedTools.join(', ')}.
- If no tool is needed, return an empty array.
- Never output tools not in allowed list.

Context:
${contextStr}

Main Output:
${mainOutput}`;
}

async function runToolChoice(mode: ToolChoiceMode, body: any) {
  const {
    main_output,
    messages,
    allowed_tools,
    model,
    temperature = 0.1,
  } = body || {};

  if (typeof main_output !== 'string' || !main_output.trim()) {
    throw new Error('main_output is required');
  }

  const allowedTools = normalizeAllowedTools(allowed_tools);
  if (allowedTools.length === 0) {
    throw new Error('allowed_tools is required');
  }

  const normalizedMessages = Array.isArray(messages) ? normalizeMessages(messages) : [];
  const contextStr = normalizedMessages.map((m: any) => `${m.role}: ${m.content}`).join('\n');
  const prompt = buildToolChoicePrompt(contextStr, main_output, allowedTools);

  if (mode === 'main') {
    const role: Role = 'main';
    const cfg = roleConfigs[role];
    const deepseek = createDeepSeek({ apiKey: cfg.apiKey });
    const finalModel = model || cfg.model;

    // 对齐需求：Output.object({ schema: z.object({ text, toolChoice }) })
    const result = await generateObject({
      model: deepseek(finalModel) as any,
      schema: z.object({
        schema: z.object({
          text: z.string().describe('Final assistant text output'),
          toolChoice: z.array(z.string()).describe('Selected tool names'),
        }),
      }),
      prompt,
      temperature,
    });

    const schemaObj = result.object?.schema || { text: main_output, toolChoice: [] };
    return {
      mode,
      model: finalModel,
      text: typeof schemaObj.text === 'string' ? schemaObj.text : main_output,
      toolChoice: normalizeToolChoice(schemaObj.toolChoice, allowedTools),
    };
  }

  // filter 模式：只返回 toolChoice
  {
    const role: Role = 'summary';
    const cfg = roleConfigs[role];
    const deepseek = createDeepSeek({ apiKey: cfg.apiKey });
    const finalModel = model || cfg.model;
    const result = await generateObject({
      model: deepseek(finalModel) as any,
      schema: z.object({
        toolChoice: z.array(z.string()).describe('List of tool names to be called'),
      }),
      prompt,
      temperature,
    });

    return {
      mode,
      model: finalModel,
      toolChoice: normalizeToolChoice(result.object?.toolChoice, allowedTools),
    };
  }
}

async function runRoleChat(role: Role, body: any) {
  const cfg = roleConfigs[role];
  const deepseek = createDeepSeek({ apiKey: cfg.apiKey });

  const {
    messages,
    model,
    temperature = 0.7,
    maxTokens = 1024,
  } = body || {};

  if (!Array.isArray(messages) || messages.length === 0) {
    throw new Error('messages is required');
  }

  const normalizedMessages = normalizeMessages(messages);
  if (normalizedMessages.length === 0) {
    throw new Error('no valid messages');
  }

  // 工具模型可选调用约束：callOptionalSchema.toolChoice
  if (role === 'tool' && body?.callOptionalSchema !== undefined) {
    const callOptionalSchema = body.callOptionalSchema;
    if (!callOptionalSchema || !Array.isArray(callOptionalSchema.toolChoice)) {
      throw new Error('callOptionalSchema.toolChoice is required for tool role');
    }
    const limitedTools = normalizeAllowedTools(callOptionalSchema.toolChoice);
    if (limitedTools.length === 0) {
      throw new Error('callOptionalSchema.toolChoice cannot be empty');
    }
    normalizedMessages.unshift({
      role: 'system',
      content: `You are restricted to these tools only: ${limitedTools.join(', ')}.`,
    });
  }

  const finalModel = model || cfg.model;
  const result = await generateText({
    model: deepseek(finalModel) as any,
    messages: normalizedMessages,
    temperature,
  });

  return {
    content: result.text,
    model: finalModel,
    role,
  };
}

function createRoleHandler(role: Role) {
  return async (req: any, res: any) => {
    try {
      const out = await runRoleChat(role, req.body || {});
      res.json(out);
    } catch (error: any) {
      const msg = error?.message || String(error);
      const status = msg.includes('messages') ? 400 : 500;
      res.status(status).json({ error: msg, role });
    }
  };
}

app.get('/health', (_req: any, res: any) => {
  res.json({ ok: true, service: 'ts-ai-sdk-gateway' });
});

app.post('/api/chat', async (req: any, res: any) => {
  try {
    const role = resolveRole(req.body?.role);
    const out = await runRoleChat(role, req.body || {});
    res.json(out);
  } catch (error: any) {
    res.status(500).json({
      error: error?.message || String(error),
    });
  }
});

app.post('/api/chat/main', createRoleHandler('main'));
app.post('/api/chat/tool', createRoleHandler('tool'));
app.post('/api/chat/summary', createRoleHandler('summary'));
app.post('/api/chat/bored', createRoleHandler('bored'));

app.post('/api/chat/main/stream', async (req: any, res: any) => {
  try {
    const role: Role = 'main';
    const cfg = roleConfigs[role];
    const deepseek = createDeepSeek({ apiKey: cfg.apiKey });

    const { messages, model, temperature = 0.7, maxTokens = 1024, tools } = req.body || {};

    if (!Array.isArray(messages) || messages.length === 0) {
      return res.status(400).json({ error: 'messages is required' });
    }

    const normalizedMessages = normalizeMessages(messages);
    if (normalizedMessages.length === 0) {
      return res.status(400).json({ error: 'no valid messages' });
    }

    const finalModel = model || cfg.model;
    const result = await streamText({
      model: deepseek(finalModel) as any,
      messages: normalizedMessages,
      temperature,
    });

    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    for await (const chunk of result.textStream) {
      res.write(chunk);
    }
    res.end();
  } catch (error: any) {
    res.status(500).json({ error: error?.message || String(error) });
  }
});

app.post('/api/filter_tools', async (req: any, res: any) => {
  try {
    const out = await runToolChoice('filter', req.body || {});
    res.json({
      selected_tools: out.toolChoice,
      model: out.model,
    });
  } catch (error: any) {
    res.status(500).json({ error: error?.message || String(error) });
  }
});

app.post('/api/tool_choice', async (req: any, res: any) => {
  try {
    const mode: ToolChoiceMode = req.body?.mode === 'main' ? 'main' : 'filter';
    const out = await runToolChoice(mode, req.body || {});
    res.json(out);
  } catch (error: any) {
    const msg = error?.message || String(error);
    const status = msg.includes('required') ? 400 : 500;
    res.status(status).json({ error: msg });
  }
});

app.listen(port, () => {
  console.log(`[TS AI Gateway] listening on http://127.0.0.1:${port}`);
});
