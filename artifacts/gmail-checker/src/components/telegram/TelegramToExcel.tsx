import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiUrl, isApiReachable, normalizePhone, parseApiJson } from "@/lib/apiBase";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { CheckCircle2, Copy, Download, Loader2, LogOut, MessageSquare, RefreshCw, ShieldCheck, Trash2, XCircle } from "lucide-react";

const PHONE_PREFIX = "+91";

type TelegramStatus = {
  configured: boolean;
  usingDefaults?: boolean;
  authorized: boolean;
  authStep: string;
  chatTarget: string | null;
  phone: string | null;
  excelRowCount: number;
  processedMessageCount: number;
  smtpValid?: number;
  smtpInvalid?: number;
  totalValid?: number;
  totalInvalid?: number;
  recentRows: string[][];
  lastError: string | null;
  hasSession?: boolean;
};

function pushLog(setter: React.Dispatch<React.SetStateAction<string[]>>, line: string) {
  const ts = new Date().toLocaleTimeString();
  setter(prev => [...prev.slice(-99), `[${ts}] ${line}`]);
}

export function TelegramToExcel() {
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [apiId, setApiId] = useState("");
  const [apiHash, setApiHash] = useState("");
  const [chatTarget, setChatTarget] = useState("");
  const [phoneDigits, setPhoneDigits] = useState("");
  const [showAdvancedSetup, setShowAdvancedSetup] = useState(false);
  const [code, setCode] = useState("");
  const [telegramPassword, setTelegramPassword] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncProgress, setSyncProgress] = useState<{
    active?: boolean;
    phase?: string;
    message?: string;
    tasksDone?: number;
    tasksTotal?: number;
    excelRowCount?: number;
    appendedThisRun?: number;
  } | null>(null);
  const [uiLogs, setUiLogs] = useState<string[]>([]);
  const [serverLogs, setServerLogs] = useState("");
  const [copied, setCopied] = useState(false);

  const [telegramReady, setTelegramReady] = useState<boolean | null>(null);
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [apiParserVersion, setApiParserVersion] = useState<number | null>(null);
  const [runSyncAfterLogin, setRunSyncAfterLogin] = useState(false);
  const syncFnRef = useRef<() => Promise<void>>(async () => {});
  /** Last finished sync stays phase=done. Ignore that until this click actually sees active=true. */
  const sawActiveThisRun = useRef(false);

  const checkApiOnline = useCallback(async () => {
    const reachable = await isApiReachable();
    setApiOnline(reachable);
    return reachable;
  }, []);

  const checkTelegramReady = useCallback(async () => {
    try {
      const online = await checkApiOnline();
      if (!online) {
        setTelegramReady(false);
        pushLog(setUiLogs, "API server offline — Replit mein Run (Project) dabao");
        return;
      }
      const res = await apiFetch("/api/telegram/ready", { timeoutMs: 15_000 });
      const text = await res.text();
      let data: { ok?: boolean; error?: string } = {};
      try { data = JSON.parse(text) as { ok?: boolean; error?: string }; } catch {
        throw new Error(text.slice(0, 120) || `HTTP ${res.status} empty response — API Server restart karo`);
      }
      setTelegramReady(Boolean(data.ok));
      if (!data.ok) pushLog(setUiLogs, `Telegram: ${data.error ?? "not ready"}`);
      else pushLog(setUiLogs, "Telegram library OK");
    } catch (e) {
      setTelegramReady(false);
      pushLog(setUiLogs, `Telegram check: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }, [checkApiOnline]);

  const fetchServerLogs = useCallback(async () => {
    try {
      const res = await apiFetch("/api/telegram/logs?limit=100");
      const data = await parseApiJson<{ text?: string; entries?: unknown[] }>(res);
      if (data.text) setServerLogs(data.text);
    } catch {
      // ignore — logs are optional
    }
  }, []);

  const pollLiveSync = useCallback(async () => {
    try {
      const [statusRes, progressRes] = await Promise.all([
        apiFetch("/api/telegram/status", { timeoutMs: 15_000 }),
        apiFetch("/api/telegram/sync/progress", { timeoutMs: 10_000 }),
      ]);
      const statusData = await parseApiJson<TelegramStatus & { error?: string }>(statusRes);
      if (statusRes.ok) {
        setStatus(statusData);
      }
      const progress = await parseApiJson<{
        active?: boolean;
        phase?: string;
        message?: string;
        tasksDone?: number;
        tasksTotal?: number;
        excelRowCount?: number;
        appendedThisRun?: number;
      }>(progressRes);
      setSyncProgress(progress);
      if (progress.active) {
        sawActiveThisRun.current = true;
        const total = progress.excelRowCount ?? statusData.excelRowCount ?? 0;
        const run = progress.appendedThisRun ?? 0;
        const taskLine =
          progress.tasksTotal && progress.tasksTotal > 0
            ? ` · tasks ${progress.tasksDone ?? 0}/${progress.tasksTotal}`
            : "";
        setInfo(
          progress.message ??
            `Sync chal raha hai — Excel ${total} rows (+${run} is run)${taskLine}`,
        );
      } else if (
        sawActiveThisRun.current
        && (progress.phase === "done" || progress.phase === "error")
      ) {
        sawActiveThisRun.current = false;
        setSyncing(false);
      }
      await fetchServerLogs();
    } catch {
      // ignore poll blips during long sync
    }
  }, [fetchServerLogs]);

  const refreshStatus = useCallback(async () => {
    try {
      pushLog(setUiLogs, "Status check...");
      const online = await checkApiOnline();
      if (!online) {
        throw new Error("API server reach nahi ho rahi — Replit mein Run (▶ Project) dabao, API Server workflow start hona chahiye.");
      }

      const healthRes = await apiFetch("/api/healthz", { timeoutMs: 10_000 });
      const health = await parseApiJson<{
        status?: string;
        telegram?: string;
        telegramAuthVersion?: number;
        telegramParserVersion?: number;
      }>(healthRes);
      setApiParserVersion(health.telegramParserVersion ?? null);
      pushLog(
        setUiLogs,
        `Health: ${health.status}, telegram=${health.telegram ?? "?"}, authV=${health.telegramAuthVersion ?? "OLD"}, parserV=${health.telegramParserVersion ?? "OLD"}`,
      );
      if (!health.telegramParserVersion || health.telegramParserVersion < 3) {
        setError("PURANA API — Replit Shell mein chalao: bash scripts/upgrade-api.sh (shell band mat karo)");
        pushLog(setUiLogs, `WARN: parserV=${health.telegramParserVersion ?? "OLD"} — need v3 (multi-QR fix)`);
      } else if (!health.telegramAuthVersion || health.telegramAuthVersion < 5) {
        setError("Purana API — Shell: bash scripts/restart-api.sh");
        pushLog(setUiLogs, `WARN: API authV=${health.telegramAuthVersion ?? "OLD"} — restart chahiye (need v5)`);
      } else {
        setError(prev => (prev.startsWith("PURANA API") || prev.startsWith("Purana API") ? "" : prev));
      }
      if (health.telegram === "error") {
        pushLog(setUiLogs, "WARN: Telegram lib not loaded — Shell: pnpm install");
      }

      const res = await apiFetch("/api/telegram/status", { timeoutMs: 15_000 });
      const data = await parseApiJson<TelegramStatus & { error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setStatus(data);
      if (data.phone?.startsWith(PHONE_PREFIX)) {
        setPhoneDigits(data.phone.slice(PHONE_PREFIX.length).replace(/\D/g, "").slice(0, 10));
      }
      if (data.chatTarget) setChatTarget(data.chatTarget);
      pushLog(
        setUiLogs,
        `Status OK — step=${data.authStep}, bot=${data.chatTarget ?? "default"}, defaults=${data.usingDefaults ? "yes" : "no"}`,
      );
      await fetchServerLogs();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Status load failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
    }
  }, [fetchServerLogs, checkApiOnline]);

  useEffect(() => {
    void refreshStatus();
    void checkTelegramReady();
    void (async () => {
      try {
        const res = await apiFetch("/api/telegram/sync/progress", { timeoutMs: 10_000 });
        const progress = await parseApiJson<{ active?: boolean }>(res);
        if (progress.active) {
          sawActiveThisRun.current = true;
          setSyncing(true);
          void pollLiveSync();
        }
      } catch {
        // ignore
      }
    })();
    const timer = window.setInterval(() => {
      void checkApiOnline();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [refreshStatus, checkTelegramReady, checkApiOnline, pollLiveSync]);

  useEffect(() => {
    if (!syncing) return;
    void pollLiveSync();
    const timer = window.setInterval(() => {
      void pollLiveSync();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [syncing, pollLiveSync]);

  const persistTelegramConfig = async (opts?: { silent?: boolean }) => {
    const reachable = await isApiReachable();
    if (!reachable) {
      throw new Error("API server reach nahi ho rahi — server par API chalu hona chahiye.");
    }
    const hash = apiHash.trim();
    if (!apiId.trim()) {
      throw new Error("API ID daalo — my.telegram.org/apps se");
    }
    if (hash.length !== 32) {
      throw new Error(`API Hash ${hash.length}/32 chars — poora 32-character hash paste karo`);
    }
    if (!chatTarget.trim()) {
      throw new Error("Chat / Bot username daalo (e.g. GmailFProBot)");
    }
    const res = await apiFetch("/api/telegram/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        apiId: Number(apiId),
        apiHash: hash,
        chatTarget: chatTarget.trim().replace(/^@/, ""),
        ...(phoneDigits.trim() ? { phone: normalizePhone(`${PHONE_PREFIX}${phoneDigits}`) } : {}),
      }),
    });
    const data = await parseApiJson<{ ok?: boolean; error?: string }>(res);
    if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
    if (!opts?.silent) {
      setInfo("Config save ho gayi. Ab SEND CODE dabao.");
      pushLog(setUiLogs, "Config saved OK");
    } else {
      pushLog(setUiLogs, "Config auto-saved (VPS par pehli baar zaroori hai)");
    }
    await refreshStatus();
  };

  const saveConfig = async () => {
    setLoading(true);
    setError("");
    setInfo("");
    pushLog(setUiLogs, "SAVE CONFIG...");
    try {
      await persistTelegramConfig();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Config save failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
      await fetchServerLogs();
    } finally {
      setLoading(false);
    }
  };

  const sendCode = async () => {
    const digits = phoneDigits.replace(/\D/g, "");
    if (!/^\d{10}$/.test(digits)) {
      setError("10 digit mobile number daalo (sirf number, +91 alag se hai)");
      return;
    }
    const normalizedPhone = normalizePhone(`${PHONE_PREFIX}${digits}`);
    setLoading(true);
    setError("");
    pushLog(setUiLogs, `SEND CODE → ${normalizedPhone} (30-60 sec wait...)`);
    try {
      const reachable = await isApiReachable();
      if (!reachable) {
        throw new Error("API server reach nahi ho rahi — Replit mein API Server workflow restart karo.");
      }
      if (showAdvancedSetup && apiId.trim() && apiHash.trim().length === 32 && chatTarget.trim()) {
        await persistTelegramConfig({ silent: true });
      }
      const res = await apiFetch("/api/telegram/auth/send-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: normalizedPhone }),
        timeoutMs: 90_000,
      });
      const data = await parseApiJson<{ ok?: boolean; error?: string; deliveryHint?: string; isCodeViaApp?: boolean }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo(data.deliveryHint ?? "Telegram ne code bheja — app/SMS check karo.");
      setCode("");
      pushLog(setUiLogs, `Code sent OK (${data.isCodeViaApp ? "Telegram app" : "SMS"}) — turant VERIFY dabao`);
      await fetchServerLogs();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Code send failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
      await fetchServerLogs();
    } finally {
      setLoading(false);
    }
  };

  const verifyCode = async () => {
    setLoading(true);
    setError("");
    pushLog(setUiLogs, "VERIFY CODE...");
    try {
      const res = await apiFetch("/api/telegram/auth/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      const data = await parseApiJson<{ needsPassword?: boolean; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      if (data.needsPassword) {
        setInfo("Telegram 2FA password chahiye — neeche daalo.");
        pushLog(setUiLogs, "Code OK — 2FA password chahiye");
      } else {
        setInfo("Login OK — SYNC + auto SMTP shuru...");
        pushLog(setUiLogs, "Login successful — auto SYNC...");
        setRunSyncAfterLogin(true);
      }
      await refreshStatus();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Code verify failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
      await fetchServerLogs();
    } finally {
      setLoading(false);
    }
  };

  const verifyPassword = async () => {
    setLoading(true);
    setError("");
    pushLog(setUiLogs, "VERIFY 2FA PASSWORD...");
    try {
      const res = await apiFetch("/api/telegram/auth/verify-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: telegramPassword }),
      });
      const data = await parseApiJson<{ ok?: boolean; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo("Login OK — SYNC + auto SMTP shuru...");
      pushLog(setUiLogs, "2FA OK — auto SYNC...");
      await refreshStatus();
      setRunSyncAfterLogin(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Password verify failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
      await fetchServerLogs();
    } finally {
      setLoading(false);
    }
  };

  const sync = async () => {
    if (apiParserVersion !== null && apiParserVersion < 3) {
      setError("Pehle API upgrade karo: bash scripts/upgrade-api.sh");
      pushLog(setUiLogs, "SYNC blocked — parserV OLD");
      return;
    }
    sawActiveThisRun.current = false;
    setSyncing(true);
    setError("");
    setInfo("Sync shuru — rows live dikhengi...");
    pushLog(setUiLogs, "SYNC... (max 400 tasks/run, parallel QR + batched Excel)");
    void pollLiveSync();

    let syncOk = false;
    try {
      const res = await apiFetch("/api/telegram/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ limit: 15000, maxTasks: 400 }),
        timeoutMs: 600_000,
      });
      const data = await parseApiJson<{
        appended?: number;
        scanned?: number;
        skippedDuplicates?: number;
        tasksParsed?: number;
        withTwoFa?: number;
        taskMessagesFound?: number;
        batches?: number;
        tasksRemaining?: number;
        pendingTasks?: number;
        smtpValid?: number;
        smtpInvalid?: number;
        totalValid?: number;
        totalInvalid?: number;
        error?: string;
      }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);

      syncOk = true;
      const remaining = data.tasksRemaining ?? 0;
      const taskHint = (data.taskMessagesFound ?? 0) === 0
        ? " — koi Task: email@gmail.com message nahi mila, RESET SCAN try karo ya sahi bot check karo"
        : "";
      const remainHint = remaining > 0
        ? ` — ${remaining} tasks baaki, dubara SYNC dabao`
        : "";
      setInfo(
        `Is telegram SMTP: Valid ${data.smtpValid ?? 0}, Invalid ${data.smtpInvalid ?? 0} | Total Valid ${data.totalValid ?? 0}, Total Invalid ${data.totalInvalid ?? 0}${remainHint}${taskHint}`,
      );
      pushLog(
        setUiLogs,
        `Sync+SMTP — session ${data.smtpValid ?? 0}/${data.smtpInvalid ?? 0}, total ${data.totalValid ?? 0}/${data.totalInvalid ?? 0}`,
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Sync failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
      void fetchServerLogs();
    } finally {
      // Pehle button normal — refreshStatus background mein (await se UI atakta tha)
      setSyncing(false);
    }
    if (syncOk) {
      void refreshStatus();
      void fetchServerLogs();
    }
  };

  syncFnRef.current = sync;

  useEffect(() => {
    if (!runSyncAfterLogin || !status?.authorized || syncing) return;
    setRunSyncAfterLogin(false);
    void syncFnRef.current();
  }, [runSyncAfterLogin, status?.authorized, syncing]);

  const resetSmtpSession = async () => {
    if (!window.confirm("Sirf is telegram ka SMTP Valid/Invalid zero hoga. Total results same rahenge. Continue?")) return;
    pushLog(setUiLogs, "RESET SMTP check...");
    try {
      const res = await apiFetch("/api/telegram/reset-smtp", { method: "POST" });
      const data = await parseApiJson<{ ok?: boolean; message?: string; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo(data.message ?? "SMTP check reset");
      pushLog(setUiLogs, "SMTP session reset OK");
      await refreshStatus();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Reset failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
    }
  };

  const resetTotals = async () => {
    if (!window.confirm("Total Valid aur Total Invalid zero ho jayenge. Is telegram ka SMTP check same rahega. Continue?")) return;
    pushLog(setUiLogs, "RESET totals...");
    try {
      const res = await apiFetch("/api/telegram/reset-totals", { method: "POST" });
      const data = await parseApiJson<{ ok?: boolean; message?: string; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo(data.message ?? "Totals reset");
      pushLog(setUiLogs, "Totals reset OK");
      await refreshStatus();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Reset failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
    }
  };

  const logout = async () => {
    if (!window.confirm("Telegram se logout karna hai? Dubara login karna padega.")) return;
    setLoading(true);
    setError("");
    pushLog(setUiLogs, "LOGOUT...");
    try {
      const res = await apiFetch("/api/telegram/logout", { method: "POST" });
      const data = await parseApiJson<{ ok?: boolean; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo("Logout ho gaya. Dubara login ke liye SEND CODE dabao.");
      setCode("");
      pushLog(setUiLogs, "Logged out OK");
      await refreshStatus();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Logout failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
    } finally {
      setLoading(false);
    }
  };

  const resetScan = async () => {
    if (!window.confirm("Sab Excel data delete hoga (Gmail/Pass/2FA) aur scan dubara shuru hoga. Continue?")) return;
    pushLog(setUiLogs, "RESET SCAN + CLEAR EXCEL...");
    try {
      const res = await apiFetch("/api/telegram/reset-scan", { method: "POST" });
      const data = await parseApiJson<{ ok?: boolean; message?: string; error?: string }>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      setInfo(data.message ?? "Reset complete — Excel khali");
      pushLog(setUiLogs, "Reset OK — Excel cleared");
      await refreshStatus();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Reset failed";
      setError(msg);
      pushLog(setUiLogs, `ERROR: ${msg}`);
    }
  };

  const downloadExcel = (part: "telegram" | "smtp-valid" | "smtp-invalid" | "total-valid" | "total-invalid") => {
    window.open(apiUrl(`/api/telegram/excel?part=${part}`), "_blank");
  };

  const allLogsText = [
    "=== UI LOGS ===",
    uiLogs.join("\n"),
    "",
    "=== SERVER LOGS ===",
    serverLogs || "(empty)",
  ].join("\n");

  const copyAllLogs = async () => {
    try {
      await navigator.clipboard.writeText(allLogsText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Clipboard copy fail — manually select logs");
    }
  };

  const clearLogs = async () => {
    setUiLogs([]);
    try {
      await apiFetch("/api/telegram/logs", { method: "DELETE" });
      setServerLogs("");
      pushLog(setUiLogs, "Logs cleared");
    } catch {
      pushLog(setUiLogs, "Server logs clear failed");
    }
  };

  const authorized = status?.authorized;
  const rowCount = status?.excelRowCount ?? 0;
  const smtpValid = status?.smtpValid ?? 0;
  const smtpInvalid = status?.smtpInvalid ?? 0;
  const totalValid = status?.totalValid ?? 0;
  const totalInvalid = status?.totalInvalid ?? 0;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {apiOnline === false && (
        <div className="lg:col-span-3 rounded-lg border border-red-500/40 bg-red-500/10 p-4 space-y-2">
          <p className="text-sm font-mono text-red-300 font-semibold">
            API Server band hai — Telegram kaam nahi karega jab tak restart na ho.
          </p>
          <p className="text-xs font-mono text-red-200/80 leading-relaxed">
            Replit <strong>Shell</strong> mein ye command chalao (shell band mat karo):<br />
            <code className="text-red-100 block mt-1 p-2 bg-black/30 rounded">bash scripts/shell-start.sh</code>
            Ya sirf API: <code className="text-red-100">bash scripts/shell-start.sh api</code>
            <br />Test: <code className="text-red-100">/api/healthz</code> → {`{"status":"ok"}`}
          </p>
          <Button
            variant="outline"
            size="sm"
            className="font-mono text-xs border-red-500/40"
            onClick={() => void refreshStatus()}
          >
            <RefreshCw className="w-3 h-3 mr-1" />
            API check dubara karo
          </Button>
        </div>
      )}

      <div className="lg:col-span-1 space-y-4">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-3">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              <MessageSquare className="w-4 h-4 text-primary" />
              Telegram Setup
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-[10px] text-muted-foreground font-mono leading-relaxed">
              Replit jaisa: sirf <strong>+91</strong> number → SEND CODE. Bot:{" "}
              <span className="text-primary">{status?.chatTarget ?? "GmailFProBot"}</span>
            </p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="w-full font-mono text-[10px] text-muted-foreground"
              onClick={() => setShowAdvancedSetup(v => !v)}
            >
              {showAdvancedSetup ? "Hide" : "Show"} advanced (API ID / alag bot)
            </Button>
            {showAdvancedSetup && (
              <>
                <p className="text-[10px] text-muted-foreground font-mono leading-relaxed">
                  API ID/Hash:{" "}
                  <a href="https://my.telegram.org/apps" target="_blank" rel="noreferrer" className="text-primary underline">
                    my.telegram.org/apps
                  </a>
                </p>
                <Input placeholder="API ID (e.g. 35132672)" value={apiId} onChange={e => setApiId(e.target.value)} className="font-mono text-sm" />
                <Input placeholder="API Hash (32 chars)" value={apiHash} onChange={e => setApiHash(e.target.value)} className="font-mono text-sm" />
                <p className="text-[10px] text-muted-foreground font-mono">Hash length: {apiHash.trim().length}/32</p>
                <Input placeholder="Chat / Bot (GmailFProBot)" value={chatTarget} onChange={e => setChatTarget(e.target.value)} className="font-mono text-sm" />
                <Button className="w-full font-mono" onClick={saveConfig} disabled={loading}>
                  {loading ? "SAVING..." : "SAVE CONFIG"}
                </Button>
              </>
            )}
          </CardContent>
        </Card>

        <Card className="border-border bg-card/50">
          <CardHeader className="pb-3">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              <ShieldCheck className="w-4 h-4 text-primary" />
              Telegram Login
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex gap-2">
              <div className="flex items-center px-3 rounded-md border border-border bg-muted/30 font-mono text-sm text-muted-foreground shrink-0">
                {PHONE_PREFIX}
              </div>
              <Input
                placeholder="9876543210"
                value={phoneDigits}
                onChange={e => setPhoneDigits(e.target.value.replace(/\D/g, "").slice(0, 10))}
                className="font-mono text-sm"
                inputMode="numeric"
                maxLength={10}
              />
            </div>
            <Button variant="outline" className="w-full font-mono" onClick={sendCode} disabled={loading || apiOnline === false}>
              SEND CODE
            </Button>
            <Input placeholder="Telegram code (5 digits)" value={code} onChange={e => setCode(e.target.value.replace(/\D/g, ""))} className="font-mono text-sm" maxLength={6} />
            <p className="text-[10px] text-amber-400/80 font-mono">SEND CODE ke baad Telegram app mein &quot;Login code&quot; message se 5 digit copy karo. SMS code alag ho sakta hai — app wala use karo.</p>
            <Button className="w-full font-mono" onClick={verifyCode} disabled={loading}>
              VERIFY CODE
            </Button>
            {(status?.authStep === "needs_password") && (
              <>
                <Input
                  type="password"
                  placeholder="Telegram 2FA password"
                  value={telegramPassword}
                  onChange={e => setTelegramPassword(e.target.value)}
                  className="font-mono text-sm"
                />
                <Button className="w-full font-mono" onClick={verifyPassword} disabled={loading}>
                  VERIFY 2FA PASSWORD
                </Button>
              </>
            )}
            <div className="flex items-center gap-2 text-xs font-mono flex-wrap">
              <span>Server:</span>
              {apiOnline === null
                ? <Badge variant="outline">CHECKING...</Badge>
                : apiOnline
                  ? <Badge className="bg-green-500/20 text-green-400 border-green-500/30">ONLINE</Badge>
                  : <Badge className="bg-red-500/20 text-red-400 border-red-500/30">OFFLINE</Badge>}
              <span>Telegram:</span>
              {telegramReady === null
                ? <Badge variant="outline">...</Badge>
                : telegramReady
                  ? <Badge className="bg-green-500/20 text-green-400 border-green-500/30">READY</Badge>
                  : <Badge className="bg-amber-500/20 text-amber-400 border-amber-500/30">NOT LOADED</Badge>}
              <span>Login:</span>
              {authorized
                ? <Badge className="bg-green-500/20 text-green-400 border-green-500/30">CONNECTED</Badge>
                : <Badge variant="outline">{status?.authStep?.toUpperCase() ?? "NOT LOGGED IN"}</Badge>}
            </div>
            {authorized && (
              <Button
                variant="outline"
                className="w-full font-mono text-red-400 border-red-500/40 hover:bg-red-500/10"
                onClick={() => void logout()}
                disabled={loading}
              >
                <LogOut className="w-4 h-4 mr-2" />
                LOGOUT
              </Button>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="lg:col-span-2 space-y-4">
        <div className="flex flex-wrap gap-2 items-center">
          <Button
            onClick={sync}
            disabled={syncing || !authorized || (apiParserVersion !== null && apiParserVersion < 3)}
            className="font-mono"
          >
            {syncing ? <><Loader2 className="w-4 h-4 mr-2 animate-spin" />SYNCING...</> : <><RefreshCw className="w-4 h-4 mr-2" />SYNC MESSAGES</>}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => void refreshStatus()} className="font-mono text-xs">
            Refresh
          </Button>
        </div>

        <Card className="border-border bg-card/50">
          <CardContent className="p-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">Telegram download</p>
              <p className="text-3xl font-mono leading-none mt-1">{rowCount}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" className="font-mono text-xs" disabled={rowCount === 0} onClick={() => downloadExcel("telegram")}>
                <Download className="w-3 h-3 mr-1" />Excel
              </Button>
              <Button variant="outline" size="sm" className="font-mono text-xs" onClick={() => void resetScan()} title="Sirf is telegram ki download sheet">
                Reset
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border bg-card/50">
          <CardContent className="p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">SMTP check · is telegram</p>
              <Button variant="outline" size="sm" className="font-mono text-xs h-7" onClick={() => void resetSmtpSession()}>
                Reset
              </Button>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div className="rounded-md border border-green-500/30 bg-green-500/5 px-3 py-2 flex items-center justify-between gap-2">
                <div>
                  <p className="text-[10px] font-mono uppercase text-green-400/90 flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" /> Valid
                  </p>
                  <p className="text-2xl font-mono text-green-400 leading-none mt-1">{smtpValid}</p>
                </div>
                <Button variant="outline" size="sm" className="font-mono text-xs h-7" disabled={smtpValid === 0} onClick={() => downloadExcel("smtp-valid")}>
                  <Download className="w-3 h-3 mr-1" />Excel
                </Button>
              </div>
              <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 flex items-center justify-between gap-2">
                <div>
                  <p className="text-[10px] font-mono uppercase text-red-400/90 flex items-center gap-1">
                    <XCircle className="w-3 h-3" /> Invalid
                  </p>
                  <p className="text-2xl font-mono text-red-400 leading-none mt-1">{smtpInvalid}</p>
                </div>
                <Button variant="outline" size="sm" className="font-mono text-xs h-7" disabled={smtpInvalid === 0} onClick={() => downloadExcel("smtp-invalid")}>
                  <Download className="w-3 h-3 mr-1" />Excel
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border bg-card/50">
          <CardContent className="p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">Total results · saare telegram</p>
              <Button variant="outline" size="sm" className="font-mono text-xs h-7" onClick={() => void resetTotals()}>
                Reset
              </Button>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <div className="rounded-md border border-green-500/30 bg-green-500/5 px-3 py-2 flex items-center justify-between gap-2">
                <div>
                  <p className="text-[10px] font-mono uppercase text-green-400/90 flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" /> Total Valid
                  </p>
                  <p className="text-2xl font-mono text-green-400 leading-none mt-1">{totalValid}</p>
                </div>
                <Button variant="outline" size="sm" className="font-mono text-xs h-7" disabled={totalValid === 0} onClick={() => downloadExcel("total-valid")}>
                  <Download className="w-3 h-3 mr-1" />Excel
                </Button>
              </div>
              <div className="rounded-md border border-red-500/30 bg-red-500/5 px-3 py-2 flex items-center justify-between gap-2">
                <div>
                  <p className="text-[10px] font-mono uppercase text-red-400/90 flex items-center gap-1">
                    <XCircle className="w-3 h-3" /> Total Invalid
                  </p>
                  <p className="text-2xl font-mono text-red-400 leading-none mt-1">{totalInvalid}</p>
                </div>
                <Button variant="outline" size="sm" className="font-mono text-xs h-7" disabled={totalInvalid === 0} onClick={() => downloadExcel("total-invalid")}>
                  <Download className="w-3 h-3 mr-1" />Excel
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        {error && <p className="text-sm font-mono text-red-400">{error}</p>}
        {info && <p className="text-sm font-mono text-green-400/90">{info}</p>}
        {syncing && (
          <p className="text-xs font-mono text-primary/90 animate-pulse">
            Live: {syncProgress?.active
              ? (syncProgress.message ?? "Sync chal raha hai...")
              : "SYNCING... server se progress aa raha hai"}
          </p>
        )}
        {status?.lastError && <p className="text-xs font-mono text-amber-400/80">Last: {status.lastError}</p>}

        <Card className="border-border bg-card/50 overflow-hidden">
          <div className="border-b border-border p-3 flex flex-wrap gap-2 items-center justify-between">
            <span className="text-xs font-mono uppercase text-muted-foreground">Debug Logs</span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="font-mono text-xs h-7" onClick={() => void copyAllLogs()}>
                <Copy className="w-3 h-3 mr-1" />
                {copied ? "COPIED!" : "COPY ALL"}
              </Button>
              <Button variant="ghost" size="sm" className="font-mono text-xs h-7" onClick={() => void clearLogs()}>
                <Trash2 className="w-3 h-3 mr-1" />CLEAR
              </Button>
            </div>
          </div>
          <pre className="p-3 text-[10px] font-mono text-muted-foreground overflow-auto max-h-[200px] whitespace-pre-wrap break-all">
            {allLogsText || "Logs yahan dikhenge..."}
          </pre>
        </Card>

        <Card className="border-border bg-card/50 overflow-hidden">
          <div className="border-b border-border p-3 text-xs font-mono text-muted-foreground">
            Excel preview — {rowCount} total rows
          </div>
          <div className="overflow-auto max-h-[320px]">
            {(status?.recentRows?.length ?? 0) > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="font-mono text-xs">Gmail</TableHead>
                    <TableHead className="font-mono text-xs">Pass</TableHead>
                    <TableHead className="font-mono text-xs">2FA Secret</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {status!.recentRows.map((row, i) => (
                    <TableRow key={i} className="font-mono text-sm">
                      <TableCell>{row[0]}</TableCell>
                      <TableCell className="text-muted-foreground">{row[1] ? "••••••" : "—"}</TableCell>
                      <TableCell className="text-muted-foreground truncate max-w-[200px]">{row[2] ? "••••••" : "—"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="p-8 text-center text-sm text-muted-foreground font-mono">Abhi koi data nahi — login karke SYNC dabao</p>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
