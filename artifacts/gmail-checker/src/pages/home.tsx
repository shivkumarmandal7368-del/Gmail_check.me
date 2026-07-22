import React, { useState, useEffect, useRef } from "react"
import { useCheckEmails, useGetEmailStats, useLoginCheckEmails } from "@workspace/api-client-react"
import type { EmailResult, EmailStats, LoginResult, BrowserLoginResult } from "@workspace/api-client-react"
import { getBrowserResultCategory } from "@/lib/browserResultCategory"

// Extended result type: adds "checking" in-flight status + per-account timing + original credentials
type ExtBrowserResult = Omit<BrowserLoginResult, "status"> & {
  status: BrowserLoginResult["status"] | "checking";
  category?: BrowserLoginResult["category"];
  durationMs?: number;
  /** Original password from input — preserved for export */
  password?: string;
  /** Original 2FA secret from input — preserved for export */
  totpSecret?: string;
};
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import {
  Download, Terminal, CheckCircle2, XCircle, AlertTriangle,
  HelpCircle, Activity, ShieldAlert, KeyRound, Smartphone,
  Lock, MailCheck, MailX, RefreshCw, Globe, Loader2, Trash2
} from "lucide-react"

type SmtpFilter = "all" | "valid" | "invalid" | "disabled" | "catch_all" | "unknown";
type Mode = "smtp" | "login" | "browser";
type LoginList = "opened" | "not_opened" | "delete" | "unknown";

export default function Home() {
  const [mode, setMode] = useState<Mode>("smtp");

  return (
    <div className="min-h-screen bg-background text-foreground p-4 md:p-6 font-sans">
      <div className="max-w-6xl mx-auto space-y-5">

        {/* Header */}
        <header className="flex items-center justify-between pb-4 border-b border-border">
          <div className="flex items-center gap-3">
            <div className="bg-primary/10 p-2 rounded-lg border border-primary/20">
              <Terminal className="w-5 h-5 text-primary" />
            </div>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">Vanguard MX</h1>
              <p className="text-sm text-muted-foreground font-mono">SMTP + IMAP Verification</p>
            </div>
          </div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground bg-card px-3 py-1.5 rounded-full border border-border">
            <Activity className="w-4 h-4 text-primary" />
            <span className="font-mono">System Online</span>
          </div>
        </header>

        {/* Mode Toggle */}
        <div className="flex flex-wrap gap-2">
          {([
            { id: "smtp",    label: "SMTP CHECK",    icon: <Terminal className="w-4 h-4" /> },
            { id: "login",   label: "IMAP CHECK",    icon: <KeyRound className="w-4 h-4" /> },
            { id: "browser", label: "BROWSER CHECK", icon: <Globe className="w-4 h-4" /> },
          ] as { id: Mode; label: string; icon: React.ReactNode }[]).map(m => (
            <button key={m.id} onClick={() => setMode(m.id)}
              className={cn(
                "flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-mono font-medium transition-colors",
                mode === m.id
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-card border-border text-muted-foreground hover:text-foreground hover:bg-muted/50"
              )}>
              {m.icon}{m.label}
            </button>
          ))}
        </div>

        {mode === "smtp" ? <SmtpChecker /> : mode === "login" ? <LoginChecker /> : <BrowserChecker />}
      </div>
    </div>
  );
}

/* ───────────────────────── SMTP CHECKER ───────────────────────── */
function SmtpChecker() {
  const [inputText, setInputText] = useState("");
  const [results, setResults] = useState<EmailResult[]>([]);
  const [stats, setStats] = useState<EmailStats | null>(null);
  const [filter, setFilter] = useState<SmtpFilter>("all");
  const [progress, setProgress] = useState(0);

  const checkEmailsMutation = useCheckEmails();
  const getStatsMutation = useGetEmailStats();

  const handleCheck = () => {
    if (!inputText.trim()) return;
    const rawEmails = inputText.split(/[\n,]+/).map(e => e.trim()).filter(Boolean);
    if (rawEmails.length === 0) return;
    setResults([]); setStats(null); setFilter("all"); setProgress(10);
    const iv = setInterval(() => setProgress(p => Math.min(p + 10, 90)), 500);
    checkEmailsMutation.mutate(
      { data: { emails: rawEmails } },
      {
        onSuccess: (data: any) => {
          clearInterval(iv); setProgress(100); setResults(data.results);
          getStatsMutation.mutate({ data: { results: data.results } }, { onSuccess: setStats });
        },
        onError: () => { clearInterval(iv); setProgress(0); }
      }
    );
  };

  const handleExport = () => {
    const rows = (filter === "all" ? results : results.filter(r => r.status === filter));
    download(rows.map(r => r.email).join("\n"), `emails_${filter}.txt`);
  };

  const filtered = filter === "all" ? results : results.filter(r => r.status === filter);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Input */}
      <div className="lg:col-span-1">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-3">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">Target Payload</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <Textarea
              placeholder={"email1@gmail.com\nemail2@gmail.com"}
              className="min-h-[280px] resize-y bg-background/50 font-mono text-sm leading-relaxed"
              value={inputText}
              onChange={e => setInputText(e.target.value)}
            />
            <Button className="w-full font-mono font-medium tracking-wide" size="lg"
              onClick={handleCheck} disabled={checkEmailsMutation.isPending || !inputText.trim()}>
              {checkEmailsMutation.isPending ? "VERIFYING..." : "INITIATE SCAN"}
            </Button>
          </CardContent>
        </Card>
      </div>

      {/* Results */}
      <div className="lg:col-span-2 space-y-5">
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
          <StatCard title="TOTAL" value={stats?.total ?? 0} />
          <StatCard title="VALID" value={stats?.valid ?? 0} trend="valid" icon={<CheckCircle2 className="w-4 h-4" />} />
          <StatCard title="INVALID" value={stats?.invalid ?? 0} trend="invalid" icon={<XCircle className="w-4 h-4" />} />
          <StatCard title="DISABLED" value={(stats as any)?.disabled ?? 0} trend="disabled" icon={<ShieldAlert className="w-4 h-4" />} />
          <StatCard title="CATCH-ALL" value={stats?.catchAll ?? 0} trend="catchall" icon={<AlertTriangle className="w-4 h-4" />} />
          <StatCard title="UNKNOWN" value={stats?.unknown ?? 0} trend="unknown" icon={<HelpCircle className="w-4 h-4" />} />
        </div>

        {checkEmailsMutation.isPending && (
          <div className="space-y-2">
            <div className="flex justify-between text-xs font-mono text-muted-foreground uppercase tracking-wider">
              <span>Establishing SMTP Handshakes...</span><span>{progress}%</span>
            </div>
            <Progress value={progress} className="h-1 bg-border" />
          </div>
        )}

        <Card className="border-border bg-card/50 min-h-[360px] flex flex-col overflow-hidden">
          <div className="border-b border-border p-3 flex flex-wrap gap-2 items-center justify-between bg-card">
            <div className="flex flex-wrap gap-1">
              {(["all","valid","invalid","disabled","catch_all","unknown"] as SmtpFilter[]).map(f => (
                <FilterButton key={f} active={filter === f} onClick={() => setFilter(f)}>{f.replace("_"," ").toUpperCase()}</FilterButton>
              ))}
            </div>
            <Button variant="outline" size="sm" onClick={handleExport} disabled={results.length === 0} className="font-mono text-xs h-8">
              <Download className="w-3 h-3 mr-2" />EXPORT .TXT
            </Button>
          </div>
          <div className="flex-1 overflow-auto">
            {results.length > 0 ? (
              <Table>
                <TableHeader className="bg-background/50 sticky top-0 backdrop-blur-sm z-10">
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="font-mono text-xs w-[48px] text-center sticky left-0 bg-card/80 backdrop-blur-sm z-20">#</TableHead>
                    <TableHead className="font-mono text-xs">EMAIL</TableHead>
                    <TableHead className="font-mono text-xs w-[100px]">STATUS</TableHead>
                    <TableHead className="font-mono text-xs w-[70px]">CODE</TableHead>
                    <TableHead className="font-mono text-xs min-w-[160px]">DIAGNOSTIC</TableHead>
                    <TableHead className="font-mono text-xs w-[70px]">GMAIL</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((r, idx) => (
                    <TableRow key={idx} className="font-mono text-sm">
                      <TableCell className="text-center text-muted-foreground/50 text-xs tabular-nums sticky left-0 bg-card/80 backdrop-blur-sm">{idx + 1}</TableCell>
                      <TableCell className="font-medium text-foreground/90">{r.email}</TableCell>
                      <TableCell><StatusBadge status={r.status} /></TableCell>
                      <TableCell className="text-muted-foreground">{r.smtpCode || "—"}</TableCell>
                      <TableCell className="text-muted-foreground break-words">{r.reason}</TableCell>
                      <TableCell>
                        {r.isGmail
                          ? <span className="text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded border border-primary/20">YES</span>
                          : <span className="text-xs text-muted-foreground">—</span>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <EmptyState />
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ───────────────────────── LOGIN CHECKER ───────────────────────── */
function LoginChecker() {
  const [inputText, setInputText] = useState("");
  const [results, setResults] = useState<LoginResult[]>([]);
  const [activeList, setActiveList] = useState<LoginList>("opened");
  const [progress, setProgress] = useState(0);

  const loginMutation = useLoginCheckEmails();

  const handleCheck = () => {
    if (!inputText.trim()) return;

    const credentials = inputText
      .split(/\n+/)
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        const parts = line.split(":");
        if (parts.length < 2) return null;
        const email = parts[0].trim();
        const password = (parts.length > 3 ? parts.slice(1, -1).join(":") : parts[1] ?? "").trim();
        const totpRaw = parts.length > 3 ? parts[parts.length - 1].trim() : (
          // handle email:password:totp (exactly 3 segments after splitting on first colon)
          parts.length === 3 ? parts[2].trim() : undefined
        );
        const totp = totpRaw ? totpRaw.replace(/\s+/g, "") : undefined;
        if (!email || !password) return null;
        return { email, password, ...(totp ? { totp } : {}) };
      })
      .filter(Boolean) as Array<{ email: string; password: string; totp?: string }>;

    if (credentials.length === 0) return;

    setResults([]); setProgress(10);
    const iv = setInterval(() => setProgress(p => Math.min(p + 8, 90)), 600);

    loginMutation.mutate(
      { data: { credentials } },
      {
        onSuccess: (data: any) => { clearInterval(iv); setProgress(100); setResults(data.results); },
        onError: () => { clearInterval(iv); setProgress(0); }
      }
    );
  };

  const opened = results.filter(r => r.status === "accessible");
  const notOpened = results.filter(r => r.status !== "accessible");
  const displayed = activeList === "opened" ? opened : notOpened;

  const handleExport = () => {
    download(displayed.map(r => r.email).join("\n"), `gmail_${activeList}.txt`);
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Input */}
      <div className="lg:col-span-1 space-y-3">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">Credentials Payload</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Textarea
              placeholder={"email@gmail.com:password\nemail2@gmail.com:password:2FA_SECRET"}
              className="min-h-[260px] resize-y bg-background/50 font-mono text-sm leading-relaxed"
              value={inputText}
              onChange={e => setInputText(e.target.value)}
            />
            <div className="text-xs text-muted-foreground font-mono space-y-1 bg-muted/30 rounded p-2 border border-border">
              <p className="text-foreground/70 font-medium mb-1">Format:</p>
              <p>Without 2FA:&nbsp;&nbsp;<span className="text-primary">email:password</span></p>
              <p>With 2FA:&nbsp;&nbsp;&nbsp;&nbsp;<span className="text-primary">email:password:2FA_SECRET</span></p>
            </div>
            <Button className="w-full font-mono font-medium tracking-wide" size="lg"
              onClick={handleCheck} disabled={loginMutation.isPending || !inputText.trim()}>
              {loginMutation.isPending ? "CHECKING..." : "INITIATE LOGIN CHECK"}
            </Button>
          </CardContent>
        </Card>

        {/* Summary cards */}
        {results.length > 0 && (
          <div className="grid grid-cols-2 gap-3">
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "opened" ? "border-green-500/60 bg-green-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("opened")}>
              <CardContent className="p-4 flex flex-col items-center gap-1">
                <MailCheck className={cn("w-5 h-5", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")} />
                <span className={cn("text-2xl font-mono font-bold", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")}>{opened.length}</span>
                <span className="text-[10px] font-mono uppercase tracking-widest text-muted-foreground">OPENED</span>
              </CardContent>
            </Card>
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "not_opened" ? "border-red-500/60 bg-red-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("not_opened")}>
              <CardContent className="p-4 flex flex-col items-center gap-1">
                <MailX className={cn("w-5 h-5", notOpened.length > 0 ? "text-red-400" : "text-muted-foreground/30")} />
                <span className={cn("text-2xl font-mono font-bold", notOpened.length > 0 ? "text-red-400" : "text-muted-foreground/30")}>{notOpened.length}</span>
                <span className="text-[10px] font-mono uppercase tracking-widest text-muted-foreground">NOT OPENED</span>
              </CardContent>
            </Card>
          </div>
        )}
      </div>

      {/* Results */}
      <div className="lg:col-span-2 space-y-5">
        {loginMutation.isPending && (
          <div className="space-y-2">
            <div className="flex justify-between text-xs font-mono text-muted-foreground uppercase tracking-wider">
              <span>Establishing IMAP connections...</span><span>{progress}%</span>
            </div>
            <Progress value={progress} className="h-1 bg-border" />
          </div>
        )}

        <Card className="border-border bg-card/50 min-h-[400px] flex flex-col overflow-hidden">
          <div className="border-b border-border p-3 flex items-center justify-between bg-card gap-3 flex-wrap">
            <div className="flex gap-2">
              <button
                onClick={() => setActiveList("opened")}
                className={cn(
                  "flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "opened"
                    ? "bg-green-500/10 text-green-400 border-green-500/40"
                    : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50"
                )}>
                <MailCheck className="w-3.5 h-3.5" />
                OPENED ({opened.length})
              </button>
              <button
                onClick={() => setActiveList("not_opened")}
                className={cn(
                  "flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "not_opened"
                    ? "bg-red-500/10 text-red-400 border-red-500/40"
                    : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50"
                )}>
                <MailX className="w-3.5 h-3.5" />
                NOT OPENED ({notOpened.length})
              </button>
            </div>
            <Button variant="outline" size="sm" onClick={handleExport}
              disabled={displayed.length === 0} className="font-mono text-xs h-8">
              <Download className="w-3 h-3 mr-2" />EXPORT .TXT
            </Button>
          </div>

          <div className="flex-1 overflow-auto">
            {results.length === 0 ? (
              <EmptyState icon={<KeyRound className="w-8 h-8 mb-3 opacity-50" />} label="AWAITING CREDENTIALS" />
            ) : displayed.length === 0 ? (
              <EmptyState label={`NO ${activeList === "opened" ? "OPENED" : "FAILED"} ACCOUNTS`} />
            ) : (
              <Table>
                <TableHeader className="bg-background/50 sticky top-0 backdrop-blur-sm z-10">
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="font-mono text-xs w-[48px] text-center sticky left-0 bg-card/80 backdrop-blur-sm z-20">#</TableHead>
                    <TableHead className="font-mono text-xs">EMAIL</TableHead>
                    <TableHead className="font-mono text-xs w-[110px]">STATUS</TableHead>
                    <TableHead className="font-mono text-xs min-w-[160px]">REASON</TableHead>
                    {displayed.some(r => r.totpCode) && (
                      <TableHead className="font-mono text-xs w-[120px]">TOTP CODE</TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {displayed.map((r, idx) => (
                    <TableRow key={idx} className="font-mono text-sm">
                      <TableCell className="text-center text-muted-foreground/50 text-xs tabular-nums sticky left-0 bg-card/80 backdrop-blur-sm">{idx + 1}</TableCell>
                      <TableCell className="font-medium text-foreground/90">{r.email}</TableCell>
                      <TableCell><LoginStatusBadge status={r.status} /></TableCell>
                      <TableCell className="text-muted-foreground break-words">{r.reason}</TableCell>
                      {displayed.some(x => x.totpCode) && (
                        <TableCell>
                          {r.totpCode ? (
                            <div className="flex flex-col gap-0.5">
                              <span className="text-primary font-mono font-bold tracking-widest text-sm">{r.totpCode}</span>
                              {r.totpSecondsLeft != null && (
                                <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                                  <RefreshCw className="w-2.5 h-2.5" />{r.totpSecondsLeft}s left
                                </span>
                              )}
                            </div>
                          ) : <span className="text-muted-foreground">—</span>}
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ───────────────────────── BROWSER CHECKER ───────────────────────── */
function BrowserChecker() {
  // ── localStorage keys ────────────────────────────────────────────────────
  const LS = {
    input: "vbc_input", proxy: "vbc_proxy", concurrency: "vbc_conc",
    fresh: "vbc_fresh", results: "vbc_results", total: "vbc_total",
    active: "vbc_active", savedAt: "vbc_saved_at", jobId: "vbc_job_id",
    creds: "vbc_creds",
  } as const;
  const lsGet = <T,>(key: string, fb: T): T => {
    try { const v = localStorage.getItem(key); return v != null ? JSON.parse(v) : fb; } catch { return fb; }
  };

  const [inputText,    setInputText]    = useState<string>(() => lsGet(LS.input, ""));
  const [proxyText,    setProxyText]    = useState<string>(() => lsGet(LS.proxy, ""));
  const [concurrency,  setConcurrency]  = useState<number>(() => lsGet(LS.concurrency, 3));
  const [freshProfile, setFreshProfile] = useState<boolean>(() => lsGet(LS.fresh, true));
  const [activeList,   setActiveList]   = useState<LoginList>(() => lsGet(LS.active, "opened"));
  const [selectedUnknown, setSelectedUnknown] = useState<Set<string>>(new Set());
  const [total,        setTotal]        = useState<number>(() => lsGet(LS.total, 0));
  const [jobId,        setJobId]        = useState<string | null>(() => localStorage.getItem(LS.jobId));
  const [isRunning,    setIsRunning]    = useState(false);
  const [connStatus,   setConnStatus]   = useState<"idle"|"connecting"|"connected"|"reconnecting"|"disconnected">("idle");
  const [reconnectedAt, setReconnectedAt] = useState<string | null>(null);

  const [results, setResults] = useState<ExtBrowserResult[]>(() =>
    (lsGet(LS.results, []) as ExtBrowserResult[]).map(r =>
      r.status === "checking" ? { ...r, status: "unknown" as const, reason: "Tab was closed/refreshed mid-check" } : r
    )
  );

  const [restoredAt, setRestoredAt] = useState<string | null>(() => {
    const at = localStorage.getItem(LS.savedAt);
    const saved: ExtBrowserResult[] = lsGet(LS.results, []);
    return saved.length > 0 && at ? at : null;
  });

  // refs
  const sseAbortRef      = useRef<AbortController | null>(null);
  const reconnectTimer   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeJobIdRef   = useRef<string | null>(jobId);
  const appendModeRef    = useRef(false);
  // Credential map: email → {password, totpSecret?} — persisted so exports survive page refresh
  const credsMapRef = useRef<Record<string, { password: string; totpSecret?: string }>>(
    (() => { try { const v = localStorage.getItem(LS.creds); return v ? JSON.parse(v) : {}; } catch { return {}; } })()
  );

  // ── Auto-save ─────────────────────────────────────────────────────────────
  useEffect(() => { try { localStorage.setItem(LS.input,       JSON.stringify(inputText));    } catch {} }, [inputText]);
  useEffect(() => { try { localStorage.setItem(LS.proxy,       JSON.stringify(proxyText));    } catch {} }, [proxyText]);
  useEffect(() => { try { localStorage.setItem(LS.concurrency, JSON.stringify(concurrency));  } catch {} }, [concurrency]);
  useEffect(() => { try { localStorage.setItem(LS.fresh,       JSON.stringify(freshProfile)); } catch {} }, [freshProfile]);
  useEffect(() => { try { localStorage.setItem(LS.active,      JSON.stringify(activeList));   } catch {} }, [activeList]);
  useEffect(() => { try { localStorage.setItem(LS.total,       JSON.stringify(total));        } catch {} }, [total]);
  useEffect(() => { try { if (jobId) localStorage.setItem(LS.jobId, jobId); else localStorage.removeItem(LS.jobId); } catch {} }, [jobId]);
  useEffect(() => {
    try {
      const toSave = results.filter(r => r.status !== "checking");
      if (toSave.length > 0) {
        localStorage.setItem(LS.results, JSON.stringify(toSave));
        localStorage.setItem(LS.savedAt, new Date().toLocaleTimeString());
      }
    } catch {}
  }, [results]);

  // ── On mount: restore from server ────────────────────────────────────────
  useEffect(() => {
    const saved = localStorage.getItem(LS.jobId);
    if (saved) restoreJobFromServer(saved);
    return () => {
      sseAbortRef.current?.abort();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, []);

  // ── Helpers ───────────────────────────────────────────────────────────────
  const parseProxies = (text: string) => text.split(/\n+/).map(l => l.trim()).filter(Boolean);

  const parseCredentials = (text: string) =>
    text.split(/\n+/).map(l => l.trim()).filter(Boolean).map(line => {
      const parts = line.split(":");
      if (parts.length < 2) return null;
      const email = parts[0].trim();
      const password = (parts.length > 3 ? parts.slice(1, -1).join(":") : parts[1] ?? "").trim();
      const totpRaw = parts.length === 3 ? parts[2].trim() : parts.length > 3 ? parts[parts.length - 1].trim() : undefined;
      const totp = totpRaw ? totpRaw.replace(/\s+/g, "") : undefined;
      if (!email || !password) return null;
      return { email, password, ...(totp ? { totp } : {}) };
    }).filter(Boolean) as Array<{ email: string; password: string; totp?: string }>;

  const applyJobState = (job: any) => {
    setResults(prev => {
      const merged = [...prev];
      for (const r of (job.results ?? [])) {
        const cred = credsMapRef.current[r.email];
        const withCreds: ExtBrowserResult = {
          ...r,
          password:    cred?.password,
          totpSecret:  cred?.totpSecret,
        };
        const idx = merged.findIndex(x => x.email === r.email);
        if (idx !== -1) merged[idx] = withCreds; else merged.push(withCreds);
      }
      for (const email of (job.checkingEmails ?? [])) {
        if (!merged.some(x => x.email === email))
          merged.push({ email, status: "checking" as const, reason: "Browser running…", totpCode: null });
      }
      return merged;
    });
    setTotal(t => Math.max(t, job.total ?? 0));
    setIsRunning(job.status === "running");
  };

  const restoreJobFromServer = async (id: string) => {
    try {
      const res = await fetch(`/api/jobs/${id}`);
      if (!res.ok) { setJobId(null); return; }
      const { job } = await res.json();
      if (!job) { setJobId(null); return; }
      applyJobState(job);
      setRestoredAt(new Date().toLocaleTimeString());
      if (job.status === "running") {
        setReconnectedAt(new Date().toLocaleTimeString());
        connectToJobStream(id, job.eventsCount ?? 0);
      }
    } catch (e) { console.error("[BrowserChecker] restore:", e); }
  };

  const connectToJobStream = (id: string, since = 0) => {
    sseAbortRef.current?.abort();
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    activeJobIdRef.current = id;
    setConnStatus("connecting");
    const abort = new AbortController();
    sseAbortRef.current = abort;
    (async () => {
      try {
        const resp = await fetch(`/api/jobs/${id}/stream?since=${since}`, { signal: abort.signal });
        if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
        setConnStatus("connected");
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const blocks = buf.split("\n\n"); buf = blocks.pop() ?? "";
          for (const block of blocks) {
            const dataLine = block.split("\n").find(l => l.startsWith("data: "));
            if (!dataLine) continue;
            try { handleJobEvent(JSON.parse(dataLine.slice(6))); } catch {}
          }
        }
        setConnStatus("idle"); setIsRunning(false);
      } catch (err: any) {
        if (err?.name === "AbortError") return;
        console.error("[BrowserChecker] SSE:", err);
        setConnStatus("disconnected");
        if (activeJobIdRef.current === id) scheduleReconnect(id);
      }
    })();
  };

  const scheduleReconnect = (id: string) => {
    reconnectTimer.current = setTimeout(async () => {
      if (activeJobIdRef.current !== id) return;
      setConnStatus("reconnecting");
      try {
        const res = await fetch(`/api/jobs/${id}`);
        if (!res.ok) { setConnStatus("disconnected"); return; }
        const { job } = await res.json();
        if (!job) { setConnStatus("disconnected"); return; }
        if (job.status !== "running") { applyJobState(job); setConnStatus("idle"); setIsRunning(false); return; }
        setReconnectedAt(new Date().toLocaleTimeString());
        connectToJobStream(id, job.eventsCount ?? 0);
      } catch { setConnStatus("disconnected"); }
    }, 3000);
  };

  const handleJobEvent = (evt: any) => {
    if (evt.type === "checking") {
      setResults(prev => prev.some(r => r.email === evt.email) ? prev :
        [...prev, { email: evt.email, status: "checking" as const, reason: "Browser running…", totpCode: null }]);
    } else if (evt.type === "result") {
      const { type: _t, ...result } = evt;
      const cred = credsMapRef.current[result.email];
      const withCreds: ExtBrowserResult = {
        ...result,
        password:   cred?.password,
        totpSecret: cred?.totpSecret,
      };
      setResults(prev => {
        const idx = prev.findIndex(r => r.email === result.email);
        if (idx !== -1) { const n = [...prev]; n[idx] = withCreds; return n; }
        return [...prev, withCreds];
      });
    } else if (evt.type === "done" || evt.type === "cancelled") {
      setIsRunning(false); setConnStatus("idle");
    }
  };

  const buildBody = (creds: Array<{ email: string; password: string; totp?: string }>) => {
    const proxies = parseProxies(proxyText);
    return JSON.stringify({
      credentials: creds,
      ...(proxies.length > 1 ? { proxies } : proxies.length === 1 ? { proxy: proxies[0] } : {}),
      concurrency, freshProfile,
    });
  };

  const handleCheck = async () => {
    const credentials = parseCredentials(inputText);
    if (credentials.length === 0) return;
    // Build and persist credential map so exports survive page refresh
    const newCredsMap: Record<string, { password: string; totpSecret?: string }> = {};
    for (const c of credentials) newCredsMap[c.email] = { password: c.password, totpSecret: c.totp };
    credsMapRef.current = newCredsMap;
    try { localStorage.setItem(LS.creds, JSON.stringify(newCredsMap)); } catch {}
    sseAbortRef.current?.abort();
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    appendModeRef.current = false;
    setResults([]); setTotal(credentials.length); setReconnectedAt(null); setRestoredAt(null); setSelectedUnknown(new Set());
    try {
      const res = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: buildBody(credentials) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { jobId: id } = await res.json();
      setJobId(id); activeJobIdRef.current = id; setIsRunning(true);
      connectToJobStream(id, 0);
    } catch (e) { console.error("[BrowserChecker] handleCheck:", e); }
  };

  const handleStop = async () => {
    if (!jobId) return;
    try { await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } catch {}
  };

  // Hard Refresh — immediately cancels the running job on the server (stops all Chrome
  // processes), then resets EVERYTHING to a pristine fresh-page-load state.
  const handleHardRefresh = async () => {
    // 1. Kill the SSE stream and any pending reconnect timer immediately
    sseAbortRef.current?.abort();
    sseAbortRef.current = null;
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }

    // 2. Cancel the server job (terminates all running Chrome/Python processes)
    const currentJobId = activeJobIdRef.current ?? jobId;
    activeJobIdRef.current = null;
    if (currentJobId) {
      try { await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }); } catch {}
    }

    // 3. Wipe all LS keys — input, proxy, config, results, session — everything
    Object.values(LS).forEach(k => { try { localStorage.removeItem(k); } catch {} });
    try { sessionStorage.clear(); } catch {}

    // 4. Clear all refs
    credsMapRef.current = {};
    appendModeRef.current = false;

    // 5. Reset ALL React state — identical to a fresh page load
    setResults([]);
    setTotal(0);
    setJobId(null);
    setIsRunning(false);
    setConnStatus("idle");
    setReconnectedAt(null);
    setRestoredAt(null);
    setSelectedUnknown(new Set());
    setActiveList("opened");
    setInputText("");
    setProxyText("");
    setConcurrency(3);
    setFreshProfile(true);
  };

  const startRetryJob = async (creds: Array<{ email: string; password: string; totp?: string }>) => {
    sseAbortRef.current?.abort();
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    appendModeRef.current = true;
    setTotal(t => t + creds.length);
    try {
      const res = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: buildBody(creds) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { jobId: id } = await res.json();
      setJobId(id); activeJobIdRef.current = id; setIsRunning(true);
      connectToJobStream(id, 0);
    } catch (e) { console.error("[BrowserChecker] retry:", e); setTotal(t => t - creds.length); }
  };

  const handleRetry = (email: string) => {
    const cred = credsMapRef.current[email];
    if (!cred) return;
    startRetryJob([{ email, password: cred.password, totp: cred.totpSecret }]);
  };

  const handleBulkRetryUnknown = () => {
    const creds = results
      .filter(r => getBrowserResultCategory(r) === "unknown" && r.status !== "checking")
      .flatMap(r => {
        const c = credsMapRef.current[r.email];
        return c ? [{ email: r.email, password: c.password, totp: c.totpSecret }] : [];
      });
    if (creds.length > 0) startRetryJob(creds);
  };

  const handleRetrySelected = () => {
    const creds = [...selectedUnknown].flatMap(email => {
      const c = credsMapRef.current[email];
      return c ? [{ email, password: c.password, totp: c.totpSecret }] : [];
    });
    if (creds.length > 0) { startRetryJob(creds); setSelectedUnknown(new Set()); }
  };

  const toggleUnknownSelect = (email: string) => {
    setSelectedUnknown(prev => { const n = new Set(prev); n.has(email) ? n.delete(email) : n.add(email); return n; });
  };

  const selectAllUnknown = () => setSelectedUnknown(new Set(
    results.filter(r =>
      getBrowserResultCategory(r) === "unknown" && r.status !== "checking"
    ).map(r => r.email)
  ));

  const clearSelectionUnknown = () => setSelectedUnknown(new Set());

  // isChecking = derived (true while job is running OR SSE is connecting/reconnecting)
  const isChecking = isRunning || connStatus === "connecting" || connStatus === "reconnecting";

  const inFlight      = results.filter(r => r.status === "checking");
  const opened        = results.filter(r => getBrowserResultCategory(r) === "open");
  const deleteList    = results.filter(r => getBrowserResultCategory(r) === "delete");
  const notOpened     = results.filter(r => getBrowserResultCategory(r) === "not_open");
  const unknownList   = results.filter(r => getBrowserResultCategory(r) === "unknown" && r.status !== "checking");
  const unknownRetryCount = unknownList.filter(r => !!credsMapRef.current[r.email]).length;
  const completedCount = results.filter(r => r.status !== "checking").length;
  const displayed = activeList === "opened" ? opened
    : activeList === "not_opened" ? notOpened
    : activeList === "delete" ? deleteList
    : [...inFlight, ...unknownList];
  const progress = total > 0 ? Math.round((completedCount / total) * 100) : 0;

  const statusLabel = (status: ExtBrowserResult["status"]): string => {
    const map: Record<string, string> = {
      opened: "Opened", verification_required: "Verify Required",
      wrong_password: "Wrong Password", "2fa_required": "2FA Required",
      unknown: "Unknown", checking: "Checking",
    };
    return map[status] ?? status;
  };

  const downloadCSV = (rows: ExtBrowserResult[], name: string) => {
    const csv = (s: string) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    const header = "Email,Password,2FA Secret,Result";
    const body = rows.map(r =>
      [csv(r.email), csv(r.password ?? ""), csv(r.totpSecret ?? ""), csv(statusLabel(r.status))].join(",")
    ).join("\n");
    download(header + "\n" + body, name + ".csv", "text/csv");
  };

  const downloadJSON = (rows: ExtBrowserResult[], name: string) => {
    const shaped = rows.map(r => ({
      email:           r.email,
      password:        r.password        ?? "",
      twoFactorSecret: r.totpSecret      ?? "",
      result:          statusLabel(r.status),
    }));
    download(JSON.stringify(shaped, null, 2), name + ".json", "application/json");
  };

  // Connection status indicator text/colour
  const connLabel: Record<typeof connStatus, { text: string; cls: string }> = {
    idle:         { text: "",                    cls: "" },
    connecting:   { text: "⟳ Connecting…",      cls: "text-blue-400/70" },
    connected:    { text: "● Live",              cls: "text-green-400" },
    reconnecting: { text: "⟳ Reconnecting…",    cls: "text-yellow-400" },
    disconnected: { text: "✕ Disconnected",      cls: "text-red-400/70" },
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Input */}
      <div className="lg:col-span-1 space-y-3">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              <Globe className="w-3.5 h-3.5 text-primary" />Browser Login Check
              {connStatus !== "idle" && (
                <span className={cn("ml-auto text-[10px] font-mono", connLabel[connStatus].cls)}>
                  {connLabel[connStatus].text}
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <Textarea
              placeholder={"email@gmail.com:password\nemail2@gmail.com:password:2FA_SECRET"}
              className="min-h-[180px] resize-y bg-background/50 font-mono text-sm leading-relaxed"
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              disabled={isChecking}
            />

            {/* Proxy */}
            {(() => {
              const proxies = parseProxies(proxyText);
              const count = proxies.length;
              const creds = parseCredentials(inputText).length;
              return (
                <div className={cn(
                  "rounded-lg border p-2.5 text-[11px] font-mono space-y-1",
                  count > 0
                    ? "border-green-500/40 bg-green-500/5 text-green-400/90"
                    : "border-yellow-500/40 bg-yellow-500/5 text-yellow-400/90"
                )}>
                  {count === 0 ? (
                    <><p className="font-semibold">⚠ Residential proxy required on Replit</p>
                      <p className="text-yellow-300/70">Google blocks datacenter IPs. Without proxy all checks return <span className="text-orange-400">verification_required</span>.</p></>
                  ) : count === 1 ? (
                    <>
                      <p className="font-semibold">✅ Rotating proxy detected — har account alag IP se chalega</p>
                      <p className="text-green-300/70">Har account ko unique sticky session milega → alag exit IP (scrapegw / residential rotating proxies ke liye)</p>
                    </>
                  ) : (
                    <>
                      <p className="font-semibold">🔀 {count} proxies loaded — rotation active</p>
                      <p className="text-green-300/70">
                        {creds > 0 ? `${creds} accounts → ${count} proxies (round-robin)` : "Har account ko alag IP milegi (round-robin)"}
                      </p>
                    </>
                  )}
                </div>
              );
            })()}
            <div className="space-y-1.5">
              <label className="text-[10px] font-mono uppercase tracking-widest text-yellow-400/80 flex items-center gap-1">
                <Lock className="w-3 h-3" /> Residential Proxies (ek per line)
              </label>
              <Textarea
                placeholder={"http://user:pass@host1:port\nhttp://user:pass@host2:port\nhttp://user:pass@host3:port"}
                className={cn(
                  "min-h-[80px] resize-y bg-background/50 font-mono text-xs leading-relaxed",
                  parseProxies(proxyText).length > 0 ? "border-green-500/50" : "border-yellow-500/40"
                )}
                value={proxyText}
                onChange={e => setProxyText(e.target.value)}
                disabled={isChecking}
              />
            </div>

            {/* Concurrency */}
            <div className="space-y-1.5">
              <label className="text-[10px] font-mono uppercase tracking-widest text-muted-foreground flex items-center gap-1">
                <Activity className="w-3 h-3" /> Concurrent Threads
              </label>
              <div className="flex items-center gap-2">
                <button onClick={() => setConcurrency(c => Math.max(1, c - 1))} disabled={isChecking || concurrency <= 1}
                  className="w-8 h-8 rounded border border-border bg-card text-sm font-mono font-bold hover:bg-muted/50 disabled:opacity-30 transition-colors">−</button>
                <div className="flex-1 text-center font-mono text-lg font-bold text-primary">{concurrency}</div>
                <button onClick={() => setConcurrency(c => Math.min(10, c + 1))} disabled={isChecking || concurrency >= 10}
                  className="w-8 h-8 rounded border border-border bg-card text-sm font-mono font-bold hover:bg-muted/50 disabled:opacity-30 transition-colors">+</button>
              </div>
              <p className="text-[10px] text-muted-foreground/60 font-mono text-center">{concurrency} browser{concurrency > 1 ? "s" : ""} simultaneously (1–10)</p>
            </div>

            {/* Fresh device toggle */}
            <button onClick={() => !isChecking && setFreshProfile(f => !f)} disabled={isChecking}
              className={cn("w-full rounded-lg border p-3 text-left transition-colors",
                freshProfile ? "border-blue-500/40 bg-blue-500/5" : "border-border bg-card/30 opacity-60")}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] font-mono uppercase tracking-widest text-blue-400/80 flex items-center gap-1">
                  <Smartphone className="w-3 h-3" /> Fresh Device Per Run
                </span>
                <div className={cn("w-8 h-4 rounded-full transition-colors relative", freshProfile ? "bg-blue-500" : "bg-border")}>
                  <div className={cn("absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all", freshProfile ? "left-4" : "left-0.5")} />
                </div>
              </div>
              <p className="text-[10px] font-mono text-muted-foreground/70 leading-relaxed">
                {freshProfile ? "✓ Har run mein naya device — Chrome profile + fingerprint wipe hoga" : "Same device reuse hoga (faster second check)"}
              </p>
            </button>

            <div className="text-xs text-muted-foreground font-mono bg-muted/30 rounded p-2 border border-border space-y-1">
              <p className="text-foreground/70 font-medium">Format:</p>
              <p><span className="text-primary">email:password</span></p>
              <p><span className="text-primary">email:password:2FA_SECRET</span></p>
              <p className="text-yellow-400/70 mt-1">⚠ ~20–40s per account (real browser)</p>
            </div>

            {isChecking ? (
              <Button variant="destructive" className="w-full font-mono font-medium tracking-wide" size="lg" onClick={handleStop}>
                <XCircle className="w-4 h-4 mr-2" />STOP CHECK
              </Button>
            ) : (
              <Button className="w-full font-mono font-medium tracking-wide" size="lg"
                onClick={handleCheck} disabled={!inputText.trim()}>
                <Globe className="w-4 h-4 mr-2" />OPEN BROWSER & CHECK
              </Button>
            )}
            {/* Hard Refresh — always enabled; cancels running job & resets to fresh page load */}
            <Button variant="outline" size="sm" onClick={handleHardRefresh}
              className="w-full font-mono text-xs h-8 border-red-500/30 text-red-400/80 hover:bg-red-500/10 hover:text-red-400 hover:border-red-500/50 transition-colors">
              <RefreshCw className="w-3 h-3 mr-1.5" />HARD REFRESH
            </Button>
          </CardContent>
        </Card>

        {(results.length > 0 || isChecking) && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "opened" ? "border-green-500/60 bg-green-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("opened")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <MailCheck className={cn("w-4 h-4", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")}>{opened.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">OPEN</span>
              </CardContent>
            </Card>
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "not_opened" ? "border-red-500/60 bg-red-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("not_opened")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <MailX className={cn("w-4 h-4", notOpened.length > 0 ? "text-red-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", notOpened.length > 0 ? "text-red-400" : "text-muted-foreground/30")}>{notOpened.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">NOT OPEN</span>
              </CardContent>
            </Card>
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "delete" ? "border-orange-500/60 bg-orange-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("delete")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <Trash2 className={cn("w-4 h-4", deleteList.length > 0 ? "text-orange-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", deleteList.length > 0 ? "text-orange-400" : "text-muted-foreground/30")}>{deleteList.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">DELETE</span>
              </CardContent>
            </Card>
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "unknown" ? "border-yellow-500/60 bg-yellow-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("unknown")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <HelpCircle className={cn("w-4 h-4", unknownList.length > 0 ? "text-yellow-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", unknownList.length > 0 ? "text-yellow-400" : "text-muted-foreground/30")}>{unknownList.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">UNKNOWN</span>
              </CardContent>
            </Card>
          </div>
        )}
      </div>

      {/* Results */}
      <div className="lg:col-span-2 space-y-5">

        {/* Reconnected to running job banner */}
        {reconnectedAt && isRunning && (
          <div className="flex items-center justify-between rounded-lg border border-green-500/30 bg-green-500/5 px-3 py-2 text-[11px] font-mono text-green-400/80">
            <span>🔄 Reconnected to running job at <span className="text-green-300">{reconnectedAt}</span> — {completedCount}/{total} done</span>
            <button onClick={() => setReconnectedAt(null)} className="ml-3 text-green-400/50 hover:text-green-400 transition-colors text-xs">✕</button>
          </div>
        )}

        {/* Session restored banner */}
        {restoredAt && !isRunning && (
          <div className="flex items-center justify-between rounded-lg border border-blue-500/30 bg-blue-500/5 px-3 py-2 text-[11px] font-mono text-blue-400/80">
            <span>💾 Session restored from <span className="text-blue-300">{restoredAt}</span> — {results.length} results loaded</span>
            <button onClick={() => setRestoredAt(null)} className="ml-3 text-blue-400/50 hover:text-blue-400 transition-colors text-xs">✕</button>
          </div>
        )}

        {/* Live progress bar */}
        {(isChecking || (results.length > 0 && results.length < total)) && (
          <div className="space-y-2">
            <div className="flex justify-between text-xs font-mono text-muted-foreground uppercase tracking-wider">
              <span className="flex items-center gap-2">
                {isChecking && <Loader2 className="w-3 h-3 animate-spin" />}
                {isChecking ? `Checking ${completedCount} / ${total} accounts… (${inFlight.length} running)` : "Done"}
              </span>
              <span>{progress}%</span>
            </div>
            <Progress value={progress} className="h-1 bg-border" />
            {isChecking && (
              <p className="text-[11px] text-muted-foreground/60 font-mono">
                {concurrency} browser{concurrency > 1 ? "s" : ""} running — tab band karo, kaam nahi rukega 🔒
              </p>
            )}
          </div>
        )}

        <Card className="border-border bg-card/50 min-h-[400px] flex flex-col overflow-hidden">
          <div className="border-b border-border p-3 flex items-center justify-between bg-card gap-3 flex-wrap">
            <div className="flex gap-2 flex-wrap">
              <button onClick={() => setActiveList("opened")}
                className={cn("flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "opened" ? "bg-green-500/10 text-green-400 border-green-500/40" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50")}>
                <MailCheck className="w-3.5 h-3.5" />OPEN ({opened.length})
              </button>
              <button onClick={() => setActiveList("not_opened")}
                className={cn("flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "not_opened" ? "bg-red-500/10 text-red-400 border-red-500/40" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50")}>
                <MailX className="w-3.5 h-3.5" />NOT OPEN ({notOpened.length})
              </button>
              <button onClick={() => setActiveList("delete")}
                className={cn("flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "delete" ? "bg-orange-500/10 text-orange-400 border-orange-500/40" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50")}>
                <Trash2 className="w-3.5 h-3.5" />DELETE ({deleteList.length})
              </button>
              <button onClick={() => setActiveList("unknown")}
                className={cn("flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "unknown" ? "bg-yellow-500/10 text-yellow-400 border-yellow-500/40" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50")}>
                <HelpCircle className="w-3.5 h-3.5" />UNKNOWN ({unknownList.length + inFlight.length})
              </button>
            </div>
            <div className="flex gap-1.5 flex-wrap">
              {activeList === "unknown" && selectedUnknown.size > 0 && !isChecking && (
                <Button variant="outline" size="sm" onClick={handleRetrySelected}
                  className="font-mono text-xs h-8 px-2 border-yellow-500/40 text-yellow-400 hover:bg-yellow-500/10">
                  <RefreshCw className="w-3 h-3 mr-1" />RETRY SELECTED ({selectedUnknown.size})
                </Button>
              )}
              {activeList === "unknown" && unknownRetryCount > 0 && !isChecking && (
                <Button variant="outline" size="sm" onClick={handleBulkRetryUnknown}
                  className="font-mono text-xs h-8 px-2 border-yellow-500/40 text-yellow-400 hover:bg-yellow-500/10">
                  <RefreshCw className="w-3 h-3 mr-1" />RETRY ALL UNKNOWN ({unknownRetryCount})
                </Button>
              )}
              <Button variant="outline" size="sm"
                onClick={() => download(
                  displayed.filter(r => r.status !== "checking").map(r =>
                    [r.email, r.password ?? "", r.totpSecret ?? "", statusLabel(r.status)].join(":")
                  ).join("\n"),
                  `gmail_${activeList}.txt`, "text/plain")}
                disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                <Download className="w-3 h-3 mr-1" />.TXT
              </Button>
              <Button variant="outline" size="sm"
                onClick={() => downloadCSV(displayed.filter(r => r.status !== "checking"), `gmail_${activeList}`)}
                disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                <Download className="w-3 h-3 mr-1" />.CSV
              </Button>
              <Button variant="outline" size="sm"
                onClick={() => downloadJSON(displayed.filter(r => r.status !== "checking"), `gmail_${activeList}`)}
                disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                <Download className="w-3 h-3 mr-1" />.JSON
              </Button>
            </div>
          </div>

          <div className="flex-1 overflow-auto">
            {results.length === 0 && !isChecking ? (
              <EmptyState icon={<Globe className="w-8 h-8 mb-3 opacity-50" />} label="AWAITING CREDENTIALS" />
            ) : displayed.length === 0 && !isChecking ? (
              <EmptyState label={`NO ${activeList === "opened" ? "OPEN" : activeList === "not_opened" ? "NOT OPEN" : activeList === "delete" ? "DELETE" : "UNKNOWN"} ACCOUNTS`} />
            ) : displayed.length === 0 && isChecking ? (
              <div className="h-full min-h-[200px] flex flex-col items-center justify-center text-muted-foreground font-mono text-sm p-8 opacity-60">
                <Loader2 className="w-8 h-8 mb-3 animate-spin opacity-50" />
                <p>Browsers running — waiting for first result...</p>
              </div>
            ) : (
              <Table>
                <TableHeader className="bg-background/50 sticky top-0 backdrop-blur-sm z-10">
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="font-mono text-xs w-[48px] text-center sticky left-0 bg-card/80 backdrop-blur-sm z-20">#</TableHead>
                    {activeList === "unknown" && (
                      <TableHead className="w-[36px] text-center">
                        <input type="checkbox"
                          checked={unknownList.length > 0 && selectedUnknown.size === unknownList.length}
                          onChange={e => e.target.checked ? selectAllUnknown() : clearSelectionUnknown()}
                          className="accent-yellow-400 cursor-pointer" />
                      </TableHead>
                    )}
                    <TableHead className="font-mono text-xs">EMAIL</TableHead>
                    <TableHead className="font-mono text-xs min-w-[110px]">PASSWORD</TableHead>
                    <TableHead className="font-mono text-xs min-w-[140px]">2FA SECRET</TableHead>
                    <TableHead className="font-mono text-xs w-[130px]">RESULT</TableHead>
                    <TableHead className="font-mono text-xs min-w-[160px]">REASON</TableHead>
                    {displayed.some(r => r.durationMs != null) && (
                      <TableHead className="font-mono text-xs w-[65px]">TIME</TableHead>
                    )}
                    {displayed.some(r => (r as any).proxySession) && (
                      <TableHead className="font-mono text-xs w-[130px]">PROXY SESSION</TableHead>
                    )}
                    {displayed.some(r => (r as any).fingerprint) && (
                      <TableHead className="font-mono text-xs min-w-[200px]">FINGERPRINT</TableHead>
                    )}
                    {displayed.some(r => r.totpCode) && (
                      <TableHead className="font-mono text-xs w-[100px]">TOTP</TableHead>
                    )}
                    <TableHead className="font-mono text-xs w-[80px]">ACTION</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {displayed.map((r, idx) => (
                    <TableRow key={r.email} className="font-mono text-sm">
                      <TableCell className="text-center text-muted-foreground/50 text-xs tabular-nums sticky left-0 bg-card/80 backdrop-blur-sm">{idx + 1}</TableCell>
                      {activeList === "unknown" && (
                        <TableCell className="text-center">
                          <input type="checkbox"
                            checked={selectedUnknown.has(r.email)}
                            onChange={() => toggleUnknownSelect(r.email)}
                            className="accent-yellow-400 cursor-pointer" />
                        </TableCell>
                      )}
                      <TableCell className="font-medium text-foreground/90">{r.email}</TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground max-w-[150px] truncate" title={r.password ?? ""}>
                        {r.password ?? <span className="opacity-30">—</span>}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-cyan-400/80 max-w-[160px] truncate" title={r.totpSecret ?? ""}>
                        {r.totpSecret ?? <span className="text-muted-foreground opacity-30">—</span>}
                      </TableCell>
                      <TableCell><BrowserStatusBadge status={r.status} /></TableCell>
                      <TableCell className="text-muted-foreground break-words">
                        {r.status === "checking"
                          ? <span className="text-blue-400/70 text-[11px] flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" />Chrome launching…</span>
                          : r.reason}
                        {(r as any).debugScreenshot && (
                          <div className="mt-2">
                            <p className={`text-[10px] font-mono mb-1 ${r.status === "opened" ? "text-green-400/70" : "text-yellow-400/70"}`}>
                              {r.status === "opened" ? "📸 Mailbox screenshot:" : "📸 Google ne kya dikhaya:"}
                            </p>
                            <img src={(r as any).debugScreenshot} alt="screenshot"
                              className="rounded border border-border max-w-[320px] w-full cursor-pointer hover:opacity-90 transition-opacity"
                              onClick={() => window.open((r as any).debugScreenshot)} />
                          </div>
                        )}
                      </TableCell>
                      {displayed.some(x => x.durationMs != null) && (
                        <TableCell className="text-[11px] font-mono tabular-nums text-muted-foreground">
                          {r.durationMs != null
                            ? <span className={r.durationMs < 60000 ? "text-green-400/80" : "text-yellow-400/80"}>{Math.round(r.durationMs / 1000)}s</span>
                            : r.status === "checking" ? <Loader2 className="w-3 h-3 animate-spin text-blue-400/60" /> : "—"}
                        </TableCell>
                      )}
                      {displayed.some(x => (x as any).proxySession) && (
                        <TableCell className="text-[11px] font-mono text-cyan-400/80 tabular-nums">
                          <span className="text-muted-foreground/50 text-[9px]">session-</span>{(r as any).proxySession ?? "—"}
                        </TableCell>
                      )}
                      {displayed.some(x => (x as any).fingerprint) && (
                        <TableCell className="text-[10px] font-mono text-purple-400/80 leading-relaxed">
                          {(r as any).fingerprint ?? "—"}
                        </TableCell>
                      )}
                      {displayed.some(x => x.totpCode) && (
                        <TableCell>
                          {r.totpCode
                            ? <span className="text-primary font-mono font-bold tracking-widest">{r.totpCode}</span>
                            : <span className="text-muted-foreground">—</span>}
                        </TableCell>
                      )}
                      <TableCell>
                        {getBrowserResultCategory(r) === "unknown" && r.status !== "checking" && !isChecking ? (
                          <button onClick={() => handleRetry(r.email)}
                            className="flex items-center gap-1 text-[10px] font-mono px-2 py-1 rounded border border-border hover:bg-muted/50 text-muted-foreground hover:text-foreground transition-colors">
                            <RefreshCw className="w-2.5 h-2.5" />RETRY
                          </button>
                        ) : <span className="text-muted-foreground/30">—</span>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}

/* ───────────────────────── SHARED HELPERS ───────────────────────── */
function download(text: string, filename: string, mime = "text/plain") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

function EmptyState({ icon, label }: { icon?: React.ReactNode; label?: string } = {}) {
  return (
    <div className="h-full min-h-[200px] flex flex-col items-center justify-center text-muted-foreground font-mono text-sm p-8 opacity-50">
      {icon ?? <Terminal className="w-8 h-8 mb-3 opacity-50" />}
      <p>{label ?? "AWAITING PAYLOAD"}</p>
    </div>
  );
}

function StatCard({ title, value, trend, icon }: { title: string; value: number; trend?: string; icon?: React.ReactNode }) {
  const isZero = value === 0;
  return (
    <Card className="bg-card/40 border-border">
      <CardContent className="p-4 flex flex-col gap-1">
        <div className="flex items-center justify-between text-muted-foreground">
          <span className="text-[10px] font-mono tracking-widest uppercase">{title}</span>
          {icon && <span className={cn("opacity-60",
            !isZero && trend === "valid" ? "text-valid" : "",
            !isZero && trend === "invalid" ? "text-invalid" : "",
            !isZero && trend === "catchall" ? "text-catchall" : ""
          )}>{icon}</span>}
        </div>
        <span className={cn("text-2xl font-mono font-medium",
          isZero ? "text-muted-foreground/30" :
          trend === "valid" ? "text-valid" :
          trend === "invalid" ? "text-invalid" :
          trend === "disabled" ? "text-orange-400" :
          trend === "catchall" ? "text-catchall" : "text-foreground"
        )}>{value.toLocaleString()}</span>
      </CardContent>
    </Card>
  );
}

function FilterButton({ active, children, onClick, className }: { active?: boolean; children: React.ReactNode; onClick: () => void; className?: string }) {
  return (
    <button onClick={onClick} data-active={active}
      className={cn(
        "px-3 py-1.5 text-xs font-mono font-medium rounded-md transition-colors border",
        active ? "bg-secondary text-secondary-foreground border-border" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50 hover:text-foreground",
        className
      )}>
      {children}
    </button>
  );
}

function StatusBadge({ status }: { status: EmailResult["status"] | "disabled" }) {
  if (status === "disabled") return <Badge variant="outline" className="uppercase font-mono tracking-wider text-[10px] py-0 border-orange-500/50 text-orange-400 bg-orange-400/10">DISABLED</Badge>;
  const props = {
    valid: { variant: "valid", label: "VALID" },
    invalid: { variant: "invalid", label: "INVALID" },
    catch_all: { variant: "catchall", label: "CATCH-ALL" },
    unknown: { variant: "unknown", label: "UNKNOWN" },
  }[status as string] || { variant: "outline", label: (status as string).toUpperCase() };
  return <Badge variant={props.variant as any} className="uppercase font-mono tracking-wider text-[10px] py-0">{props.label}</Badge>;
}

function BrowserStatusBadge({ status }: { status: BrowserLoginResult["status"] | "checking" }) {
  const map: Record<string, { label: string; className: string; icon: React.ReactNode }> = {
    checking:              { label: "CHECKING",  className: "border-blue-500/40 text-blue-400 bg-blue-400/10",      icon: <Loader2 className="w-3 h-3 animate-spin" /> },
    opened:                { label: "OPENED",    className: "border-green-500/50 text-green-400 bg-green-400/10",   icon: <MailCheck className="w-3 h-3" /> },
    verification_required: { label: "VERIFY",    className: "border-yellow-500/50 text-yellow-400 bg-yellow-400/10", icon: <Smartphone className="w-3 h-3" /> },
    wrong_password:        { label: "BAD PASS",  className: "border-red-500/50 text-red-400 bg-red-400/10",         icon: <XCircle className="w-3 h-3" /> },
    "2fa_required":        { label: "2FA NEEDED",className: "border-blue-500/50 text-blue-400 bg-blue-400/10",      icon: <Lock className="w-3 h-3" /> },
    unknown:               { label: "UNKNOWN",   className: "border-border text-muted-foreground",                  icon: <HelpCircle className="w-3 h-3" /> },
  };
  const cfg = map[status] ?? map.unknown;
  return (
    <Badge variant="outline" className={cn("uppercase font-mono tracking-wider text-[10px] py-0 flex items-center gap-1 w-fit", cfg.className)}>
      {cfg.icon}{cfg.label}
    </Badge>
  );
}

function LoginStatusBadge({ status }: { status: LoginResult["status"] }) {
  const map: Record<string, { label: string; className: string; icon: React.ReactNode }> = {
    accessible:            { label: "OPENED",       className: "border-green-500/50 text-green-400 bg-green-400/10",  icon: <MailCheck className="w-3 h-3" /> },
    verification_required: { label: "VERIFY",        className: "border-yellow-500/50 text-yellow-400 bg-yellow-400/10", icon: <AlertTriangle className="w-3 h-3" /> },
    wrong_password:        { label: "BAD PASS",      className: "border-red-500/50 text-red-400 bg-red-400/10",        icon: <XCircle className="w-3 h-3" /> },
    app_password_required: { label: "2FA / APP PWD", className: "border-blue-500/50 text-blue-400 bg-blue-400/10",     icon: <Smartphone className="w-3 h-3" /> },
    unknown:               { label: "UNKNOWN",       className: "border-border text-muted-foreground",                 icon: <Lock className="w-3 h-3" /> },
  };
  const cfg = map[status] ?? map.unknown;
  return (
    <Badge variant="outline" className={cn("uppercase font-mono tracking-wider text-[10px] py-0 flex items-center gap-1 w-fit", cfg.className)}>
      {cfg.icon}{cfg.label}
    </Badge>
  );
}
