import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { generateText } from 'ai';
import { createDeepSeek } from '@ai-sdk/deepseek';

declare const process: any;

const app = express();
app.use(cors());
app.use(express.json({ limit: '2mb' }));

const port = Number(process.env.PORT || 3100);

type Role = 'main' | 'tool' | 'summary' | 'bored';

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

app.listen(port, () => {
  console.log(`[TS AI Gateway] listening on http://127.0.0.1:${port}`);
});
