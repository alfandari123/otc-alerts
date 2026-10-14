// Schwab login helper for the OTC bot (Cloudflare Worker, free plan).
//
// Schwab's API needs a browser login every 7 days. Reuven taps the link the bot sends him in Telegram,
// logs in at Schwab, and Schwab sends him back here (/callback). The worker keeps the tokens (KV) and hands
// a fresh 30-minute access token to the bot (GitHub Actions) on /token. Only this worker ever refreshes,
// so two runs can never fight over the refresh token.
//
// Secrets (wrangler secret put): SCHWAB_APP_KEY, SCHWAB_SECRET (from developer.schwab.com),
// LOGIN_KEY (in the login link), BOT_KEY (header X-Bot-Key from the bot).

const AUTH = "https://api.schwabapi.com/v1/oauth/authorize";
const TOKEN = "https://api.schwabapi.com/v1/oauth/token";
const REFRESH_DAYS = 7;

const page = (title, body, ok = true) => new Response(
  `<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b0e14;color:#d7dce5;font:17px/1.6 -apple-system,"Segoe UI",Arial,sans-serif;text-align:center;padding:24px}
.c{max-width:420px}.i{font-size:56px}h1{font-size:22px;margin:8px 0;color:${ok ? "#5ee0c4" : "#ff8a80"}}p{color:#8a93a6}</style></head>
<body><div class="c"><div class="i">${ok ? "✅" : "⚠️"}</div><h1>${title}</h1><p>${body}</p></div></body></html>`,
  { status: ok ? 200 : 400, headers: { "content-type": "text/html; charset=utf-8" } });

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json" } });

function callback(env, url) {
  return env.CALLBACK_URL || `${url.origin}/callback`;
}

async function tokenCall(env, form) {
  const r = await fetch(TOKEN, {
    method: "POST",
    headers: {
      Authorization: "Basic " + btoa(`${env.SCHWAB_APP_KEY}:${env.SCHWAB_SECRET}`),
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: new URLSearchParams(form),
  });
  const text = await r.text();
  let data = {};
  try { data = JSON.parse(text); } catch { /* not JSON */ }
  if (!r.ok || !data.access_token) throw new Error(`schwab ${r.status} ${(data.error_description || data.error || text).slice(0, 120)}`);
  return data;
}

async function save(env, data, fresh) {
  const now = Date.now();
  const old = (await env.KV.get("tok", "json")) || {};
  const tok = {
    access: data.access_token,
    access_exp: now + (data.expires_in || 1800) * 1000,
    refresh: data.refresh_token || old.refresh,
    // the 7-day clock starts at the browser login, not at later refreshes
    login_ts: fresh ? now : old.login_ts,
  };
  await env.KV.put("tok", JSON.stringify(tok));
  return tok;
}

function configured(env) {
  return Boolean(env.SCHWAB_APP_KEY && env.SCHWAB_SECRET);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname;

    if (path === "/login") {
      if (url.searchParams.get("k") !== env.LOGIN_KEY) return page("קישור לא תקין", "פתח את הקישור מהבוט בטלגרם.", false);
      if (!configured(env)) return page("עוד לא מוגדר", "החיבור ל-Schwab עוד לא הוגדר (חסרים פרטי האפליקציה).", false);
      const state = crypto.randomUUID();
      await env.KV.put("state:" + state, "1", { expirationTtl: 600 });
      const to = `${AUTH}?client_id=${encodeURIComponent(env.SCHWAB_APP_KEY)}&redirect_uri=${encodeURIComponent(callback(env, url))}&state=${state}`;
      return Response.redirect(to, 302);
    }

    if (path === "/callback") {
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state");
      if (!code) return page("הכניסה לא הושלמה", "Schwab לא החזירו אישור. נסה שוב מהקישור בבוט.", false);
      if (!state || !(await env.KV.get("state:" + state))) return page("הקישור פג", "התחל שוב מהקישור בבוט בטלגרם.", false);
      await env.KV.delete("state:" + state);
      try {
        await save(env, await tokenCall(env, { grant_type: "authorization_code", code, redirect_uri: callback(env, url) }), true);
      } catch (e) {
        return page("החיבור נכשל", "Schwab דחו את הבקשה. נסה שוב בעוד דקה. (" + String(e.message).replace(/[<>&]/g, "") + ")", false);
      }
      return page("מחובר ל-Schwab", "הבוט יכול לסחור ב-7 הימים הקרובים. אפשר לסגור את הדף. תקבל תזכורת בטלגרם לפני שהחיבור יפוג.");
    }

    // ----- bot endpoints -----
    if (req.headers.get("X-Bot-Key") !== env.BOT_KEY || !env.BOT_KEY) return json({ error: "forbidden" }, 403);
    const tok = (await env.KV.get("tok", "json")) || null;
    const ageDays = tok && tok.login_ts ? (Date.now() - tok.login_ts) / 86400000 : null;

    if (path === "/status") {
      return json({ configured: configured(env), connected: Boolean(tok && tok.refresh && ageDays < REFRESH_DAYS),
                    login_ts: tok ? tok.login_ts : null, age_days: ageDays, days_left: ageDays == null ? null : REFRESH_DAYS - ageDays });
    }

    if (path === "/token") {
      if (!tok || !tok.refresh) return json({ error: "login_needed" }, 401);
      if (tok.access && tok.access_exp - Date.now() > 120000) return json({ access_token: tok.access, days_left: REFRESH_DAYS - ageDays });
      if (ageDays >= REFRESH_DAYS) return json({ error: "login_needed", expired: true }, 401);
      try {
        const t = await save(env, await tokenCall(env, { grant_type: "refresh_token", refresh_token: tok.refresh }), false);
        return json({ access_token: t.access, days_left: REFRESH_DAYS - ageDays });
      } catch (e) {
        return json({ error: "refresh_failed", detail: String(e.message) }, 502);
      }
    }

    return json({ error: "not found" }, 404);
  },
};
