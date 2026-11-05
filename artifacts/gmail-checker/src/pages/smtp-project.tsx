import React, { useState, useEffect, useRef } from "react"
import * as XLSX from "xlsx"
import { useLoginCheckEmails } from "@workspace/api-client-react"
import type { EmailResult, EmailStats, LoginResult, BrowserLoginResult } from "@workspace/api-client-react"
import { getBrowserResultCategory } from "@/lib/browserResultCategory"
import {
  credentialsToInputText,
  parseCredentialsFromFile,
  parseSmtpPayload,
  type ParsedCredential,
  type SmtpEntry,
} from "@/lib/parseCredentialFile"
import { apiFetch, isApiReachable } from "@/lib/apiBase"
import { TelegramToExcel } from "@/components/telegram/TelegramToExcel"
import { GmailPaySave } from "@/components/gmailPay/GmailPaySave"
import { NumberedPayloadEditor } from "@/components/smtp/NumberedPayloadEditor"

// Extended result type: adds "checking" in-flight status + per-account timing + original credentials
type ExtBrowserResult = Omit<BrowserLoginResult, "status"> & {
  status: BrowserLoginResult["status"] | "checking";
  category?: BrowserLoginResult["category"];
  durationMs?: number;
  /** Persisted locally in App Password mode so refresh keeps the generated secret. */
  appPassword?: string;
  /** Original password from input — preserved for export */
  password?: string;
  /** Original 2FA secret from input — preserved for export */
  totpSecret?: string;
};

type BrowserDebugEvent = {
  type?: "debug";
  timestamp: number;
  account: string;
  level: "info" | "warn" | "error";
  category: "account" | "step" | "network" | "proxy" | "url" | "result" | "message";
  message: string;
  event?: string;
  step?: string;
  previousStep?: string;
  durationMs?: number;
  totalDurationMs?: number;
  url?: string;
  ip?: string;
  previousIp?: string;
  retry?: number;
  retries?: number;
  sessionId?: string;
  proxyUsername?: string;
  proxyEndpoint?: string;
};

/** Google shows app passwords as four groups of four letters: xxxx xxxx xxxx xxxx */
function formatAppPasswordDisplay(value: string): string {
  const compact = value.replace(/\s+/g, "").toLowerCase();
  if (!/^[a-z]{16}$/.test(compact)) return value;
  return `${compact.slice(0, 4)} ${compact.slice(4, 8)} ${compact.slice(8, 12)} ${compact.slice(12, 16)}`;
}

function CopyIconButton({
  value,
  copyKey,
  copiedKey,
  onCopy,
  className,
}: {
  value?: string | null;
  copyKey: string;
  copiedKey: string | null;
  onCopy: (value: string, copyKey: string) => void;
  className?: string;
}) {
  if (!value) return null;
  return (
    <button
      type="button"
      onClick={() => onCopy(value, copyKey)}
      title="Copy"
      className={cn(
        "shrink-0 p-1 rounded border border-border/60 text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors",
        className,
      )}
    >
      {copiedKey === copyKey
        ? <CheckCircle2 className="w-3 h-3 text-green-400" />
        : <Copy className="w-3 h-3" />}
    </button>
  );
}
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
  Lock, MailCheck, MailX, RefreshCw, Globe, Loader2, Trash2, PauseCircle, PlayCircle, Zap, Copy, Upload, MessageSquare, Wallet
} from "lucide-react"

type SmtpFilter = "all" | "valid" | "invalid" | "disabled" | "catch_all" | "unknown";
type Mode = "smtp" | "login" | "browser" | "app_password" | "telegram" | "gmail_pay";
type LoginList = "opened" | "not_opened" | "delete" | "unknown" | "fingerprint";

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
            { id: "app_password", label: "CREATE APP PASSWORD", icon: <KeyRound className="w-4 h-4" /> },
            { id: "telegram", label: "TELEGRAM → EXCEL", icon: <MessageSquare className="w-4 h-4" /> },
            { id: "gmail_pay", label: "GMAIL PAY SAVE", icon: <Wallet className="w-4 h-4" /> },
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

        {mode === "smtp" ? <SmtpChecker /> : mode === "login" ? <LoginChecker /> : mode === "telegram" ? <TelegramToExcel /> : mode === "gmail_pay" ? <GmailPaySave /> : (
          <BrowserChecker
            key={mode}
            action={mode === "app_password" ? "create_app_password" : "check"}
          />
        )}
      </div>
    </div>
  );
}

/* ───────────────────────── SMTP CHECKER ───────────────────────── */
type SmtpRowResult = EmailResult & {
  series: number;
  password?: string;
  totpSecret?: string;
};

const SMTP_MAX_ENTRIES = 500;
const SMTP_CHUNK_SIZE = 35;

function smtpStatsFromResults(results: EmailResult[]): EmailStats {
  const total = results.length;
  const valid = results.filter(r => r.status === "valid").length;
  const invalid = results.filter(r => r.status === "invalid").length;
  const disabled = results.filter(r => r.status === "disabled").length;
  const catchAll = results.filter(r => r.status === "catch_all").length;
  const unknown = results.filter(r => r.status === "unknown").length;
  return {
    total,
    valid,
    invalid,
    disabled,
    catchAll,
    unknown,
    validPercent: total > 0 ? Math.round((valid / total) * 100) : 0,
    invalidPercent: total > 0 ? Math.round((invalid / total) * 100) : 0,
  };
}

function enrichSmtpResults(batch: EmailResult[], chunkEntries: SmtpEntry[]): SmtpRowResult[] {
  return batch.map((row, idx) => {
    const entry =
      chunkEntries[idx]?.email.toLowerCase() === row.email.toLowerCase()
        ? chunkEntries[idx]
        : chunkEntries.find(e => e.email.toLowerCase() === row.email.toLowerCase());
    return {
      ...row,
      series: entry?.series ?? chunkEntries[idx]?.series ?? idx + 1,
      password: entry?.password,
      totpSecret: entry?.totp,
    };
  });
}

const SMTP_EXPORT_SHEETS: { status: EmailResult["status"]; sheetName: string }[] = [
  { status: "valid", sheetName: "Valid" },
  { status: "invalid", sheetName: "Invalid" },
  { status: "disabled", sheetName: "Disabled" },
  { status: "catch_all", sheetName: "Catch-all" },
  { status: "unknown", sheetName: "Unknown" },
];

function smtpCredentialExportRows(rows: SmtpRowResult[]): (string | number)[][] {
  return rows.map(r => [r.series, r.email, r.password ?? "", r.totpSecret ?? ""]);
}

function downloadSmtpWorkbookByStatus(allResults: SmtpRowResult[]) {
  const workbook = XLSX.utils.book_new();
  const headers = ["#", "Gmail", "Password", "2FA Secret"];
  let sheetCount = 0;

  for (const { status, sheetName } of SMTP_EXPORT_SHEETS) {
    const rows = allResults
      .filter(r => r.status === status)
      .sort((a, b) => a.series - b.series);
    if (rows.length === 0) continue;
    const worksheet = XLSX.utils.aoa_to_sheet([headers, ...smtpCredentialExportRows(rows)]);
    XLSX.utils.book_append_sheet(workbook, worksheet, sheetName.slice(0, 31));
    sheetCount += 1;
  }

  if (sheetCount === 0) return;

  const buffer = XLSX.write(workbook, { bookType: "xlsx", type: "array" });
  downloadBlob(
    new Blob([buffer], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }),
    "smtp_by_status.xlsx",
  );
}

function SmtpChecker() {
  const [inputText, setInputText] = useState("");
  const [results, setResults] = useState<SmtpRowResult[]>([]);
  const [stats, setStats] = useState<EmailStats | null>(null);
  const [filter, setFilter] = useState<SmtpFilter>("all");
  const [progress, setProgress] = useState(0);
  const [progressLabel, setProgressLabel] = useState("");
  const [isChecking, setIsChecking] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [checkError, setCheckError] = useState("");
  const [uploadedFileName, setUploadedFileName] = useState<string | null>(null);
  const credentialFileRef = useRef<HTMLInputElement | null>(null);
  const entriesRef = useRef<SmtpEntry[]>([]);
  const checkAbortRef = useRef<AbortController | null>(null);

  const lineCount = inputText ? inputText.split("\n").length : 0;
  const parsedPreview = parseSmtpPayload(inputText);
  const validLineCount = parsedPreview.length;

  const handleClear = () => {
    checkAbortRef.current?.abort();
    checkAbortRef.current = null;
    setInputText("");
    setResults([]);
    setStats(null);
    setFilter("all");
    setProgress(0);
    setProgressLabel("");
    setIsChecking(false);
    setUploadError("");
    setCheckError("");
    setUploadedFileName(null);
    entriesRef.current = [];
  };

  const handleCheck = async () => {
    const entries = parseSmtpPayload(inputText);
    if (entries.length === 0) {
      setCheckError("Kam se kam ek valid email line daalo");
      return;
    }
    if (entries.length > SMTP_MAX_ENTRIES) {
      setCheckError(`Max ${SMTP_MAX_ENTRIES} accounts ek scan mein — abhi ${entries.length} hain`);
      return;
    }

    checkAbortRef.current?.abort();
    const abort = new AbortController();
    checkAbortRef.current = abort;

    entriesRef.current = entries;
    setCheckError("");
    setResults([]);
    setStats(null);
    setFilter("all");
    setProgress(0);
    setProgressLabel(`0 / ${entries.length} checked`);
    setIsChecking(true);

    const allRaw: EmailResult[] = [];

    try {
      for (let offset = 0; offset < entries.length; offset += SMTP_CHUNK_SIZE) {
        if (abort.signal.aborted) break;

        const chunkEntries = entries.slice(offset, offset + SMTP_CHUNK_SIZE);
        const res = await apiFetch("/api/emails/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ emails: chunkEntries.map(e => e.email) }),
          signal: abort.signal,
          timeoutMs: 120_000,
        });
        if (!res.ok) {
          const errBody = await res.text().catch(() => "");
          throw new Error(errBody.slice(0, 120) || `HTTP ${res.status}`);
        }
        const data = await res.json() as { results: EmailResult[] };
        const batch = data.results ?? [];
        allRaw.push(...batch);

        const enriched = enrichSmtpResults(batch, chunkEntries);
        setResults(prev => [...prev, ...enriched].sort((a, b) => a.series - b.series));
        setStats(smtpStatsFromResults(allRaw));

        const done = Math.min(offset + chunkEntries.length, entries.length);
        setProgress(Math.round((done / entries.length) * 100));
        setProgressLabel(`${done} / ${entries.length} checked`);
      }
      setProgress(100);
      setProgressLabel(`${entries.length} / ${entries.length} done`);
    } catch (e) {
      if (abort.signal.aborted) {
        setCheckError("Scan cancel / clear ho gaya");
      } else {
        const msg = e instanceof Error ? e.message : "SMTP scan failed";
        setCheckError(msg);
        if (allRaw.length > 0) {
          setStats(smtpStatsFromResults(allRaw));
        }
      }
    } finally {
      if (checkAbortRef.current === abort) checkAbortRef.current = null;
      setIsChecking(false);
    }
  };

  const handleCredentialUpload = async (file: File) => {
    try {
      const credentials = await parseCredentialsFromFile(file);
      if (credentials.length === 0) throw new Error("Excel/CSV mein koi valid row nahi mili");
      const entries: SmtpEntry[] = credentials.map((c, index) => ({
        series: index + 1,
        email: c.email,
        password: c.password,
        ...(c.totp ? { totp: c.totp } : {}),
      }));
      entriesRef.current = entries;
      setInputText(
        entries
          .map(entry => {
            const payload = [entry.email, entry.password, entry.totp].filter(Boolean).join(":");
            return payload;
          })
          .join("\n"),
      );
      setUploadedFileName(file.name);
      setUploadError("");
      setCheckError("");
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "File read nahi ho payi");
      setUploadedFileName(null);
    }
  };

  const handleExportTxt = () => {
    const rows = filter === "all" ? results : results.filter(r => r.status === filter);
    download(
      rows
        .sort((a, b) => a.series - b.series)
        .map(r => {
          const cred = [r.email, r.password ?? "", r.totpSecret ?? ""].filter((part, i) => i === 0 || part).join(":");
          return `${r.series}:${cred}:${r.status}:${r.smtpCode || ""}:${r.reason || ""}`;
        })
        .join("\n"),
      `smtp_${filter}.txt`,
    );
  };

  const filtered = (filter === "all" ? results : results.filter(r => r.status === filter))
    .sort((a, b) => a.series - b.series);
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-1">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-3">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground">Target Payload</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <NumberedPayloadEditor
              value={inputText}
              onChange={v => {
                setInputText(v);
                setUploadError("");
                setCheckError("");
              }}
              disabled={isChecking}
              placeholder={"Excel copy (TAB):  email@gmail.com    password    2fa_key\nExample: matthewmartinlbc339@gmail.com + pass + 3qtbv32u...\nAlso : and | — sirf email bhi chalega"}
            />
            <p className="text-[10px] text-muted-foreground/80 font-mono leading-relaxed flex flex-wrap gap-x-3 gap-y-1">
              <span>Lines: <strong className="text-foreground/90">{lineCount}</strong></span>
              <span>Valid entries: <strong className="text-primary">{validLineCount}</strong></span>
              <span>With pass+2FA: <strong className="text-green-400/90">{parsedPreview.filter(e => e.password && e.totp).length}</strong></span>
              <span>Max/scan: {SMTP_MAX_ENTRIES}</span>
            </p>
            <p className="text-[10px] text-muted-foreground/80 font-mono leading-relaxed">
              Sabse common: <span className="text-primary">Gmail [TAB] Pass [TAB] 2FA</span> (Excel se direct paste).
              SMTP sirf mailbox check; pass/2FA export sheets mein <span className="text-green-400/80">same line se copy</span> (Valid / Invalid / Disabled alag sheets).
            </p>
            <input
              ref={credentialFileRef}
              type="file"
              accept=".xlsx,.xls,.csv,.txt"
              className="hidden"
              onChange={e => {
                const file = e.target.files?.[0];
                if (file) void handleCredentialUpload(file);
                e.target.value = "";
              }}
            />
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="font-mono text-xs"
                onClick={() => credentialFileRef.current?.click()}
                disabled={isChecking}
              >
                <Upload className="w-3 h-3 mr-2" />UPLOAD EXCEL
              </Button>
              {uploadedFileName && (
                <span className="text-[10px] font-mono text-green-400/80 self-center truncate max-w-[180px]">
                  {uploadedFileName}
                </span>
              )}
            </div>
            {uploadError && (
              <p className="text-[11px] font-mono text-red-400/90">{uploadError}</p>
            )}
            {checkError && (
              <p className="text-[11px] font-mono text-red-400/90">{checkError}</p>
            )}
            <div className="flex gap-2">
              <Button className="flex-1 font-mono font-medium tracking-wide" size="lg"
                onClick={handleCheck} disabled={isChecking || validLineCount === 0}>
                {isChecking ? "VERIFYING..." : "INITIATE SCAN"}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="lg"
                className="font-mono shrink-0"
                onClick={handleClear}
                disabled={!isChecking && !inputText.trim() && results.length === 0}
                title="Input + results clear (running scan ko bhi rokta hai)"
              >
                <Trash2 className="w-4 h-4" />
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="lg:col-span-2 space-y-5">
        <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
          <StatCard title="TOTAL" value={stats?.total ?? 0} />
          <StatCard title="VALID" value={stats?.valid ?? 0} trend="valid" icon={<CheckCircle2 className="w-4 h-4" />} />
          <StatCard title="INVALID" value={stats?.invalid ?? 0} trend="invalid" icon={<XCircle className="w-4 h-4" />} />
          <StatCard title="DISABLED" value={(stats as any)?.disabled ?? 0} trend="disabled" icon={<ShieldAlert className="w-4 h-4" />} />
          <StatCard title="CATCH-ALL" value={stats?.catchAll ?? 0} trend="catchall" icon={<AlertTriangle className="w-4 h-4" />} />
          <StatCard title="UNKNOWN" value={stats?.unknown ?? 0} trend="unknown" icon={<HelpCircle className="w-4 h-4" />} />
        </div>

        {isChecking && (
          <div className="space-y-2">
            <div className="flex justify-between text-xs font-mono text-muted-foreground uppercase tracking-wider">
              <span>SMTP scan — results live update ho rahe hain</span>
              <span>{progressLabel || `${progress}%`}</span>
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
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={handleExportTxt} disabled={results.length === 0} className="font-mono text-xs h-8">
                <Download className="w-3 h-3 mr-2" />EXPORT .TXT
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => downloadSmtpWorkbookByStatus(results)}
                disabled={results.length === 0}
                className="font-mono text-xs h-8 border-green-500/40 text-green-400 hover:bg-green-500/10"
              >
                <Download className="w-3 h-3 mr-2" />EXPORT .XLSX (sheets)
              </Button>
            </div>
          </div>
          <div className="flex-1 overflow-auto">
            {results.length > 0 ? (
              <Table>
                <TableHeader className="bg-background/50 sticky top-0 backdrop-blur-sm z-10">
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="font-mono text-xs w-[48px] text-center sticky left-0 bg-card/80 backdrop-blur-sm z-20">#</TableHead>
                    <TableHead className="font-mono text-xs">GMAIL</TableHead>
                    <TableHead className="font-mono text-xs w-[72px]">PASS</TableHead>
                    <TableHead className="font-mono text-xs w-[72px]">2FA</TableHead>
                    <TableHead className="font-mono text-xs w-[100px]">STATUS</TableHead>
                    <TableHead className="font-mono text-xs w-[70px]">CODE</TableHead>
                    <TableHead className="font-mono text-xs min-w-[160px]">DIAGNOSTIC</TableHead>
                    <TableHead className="font-mono text-xs w-[70px]">GMAIL</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((r) => (
                    <TableRow key={`${r.series}-${r.email}`} className="font-mono text-sm">
                      <TableCell className="text-center text-muted-foreground/80 text-xs tabular-nums sticky left-0 bg-card/80 backdrop-blur-sm font-semibold">{r.series}</TableCell>
                      <TableCell className="font-medium text-foreground/90">{r.email}</TableCell>
                      <TableCell className="text-muted-foreground text-xs">{r.password ? "••••" : "—"}</TableCell>
                      <TableCell className="text-muted-foreground text-xs">{r.totpSecret ? "••••" : "—"}</TableCell>
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
  const [notOpenedFilter, setNotOpenedFilter] = useState<"all" | "wrong_password" | "verification_required" | "app_password_required" | "unknown">("all");
  // Credential map — keeps password/TOTP for export (results don't carry passwords)
  const credsMapRef = useRef<Record<string, { password: string; totpSecret?: string }>>({});

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

    // Build credential map so exports survive without re-parsing
    const newCredsMap: Record<string, { password: string; totpSecret?: string }> = {};
    for (const c of credentials) newCredsMap[c.email] = { password: c.password, totpSecret: c.totp };
    credsMapRef.current = newCredsMap;

    setResults([]); setProgress(10); setNotOpenedFilter("all");
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
  const notOpenedAll = results.filter(r => r.status !== "accessible");
  const notOpened = notOpenedFilter === "all" ? notOpenedAll : notOpenedAll.filter(r => r.status === notOpenedFilter);
  const displayed = activeList === "opened" ? opened : notOpened;

  const handleExport = () => {
    download(
      displayed.map(r => {
        const cred = credsMapRef.current[r.email];
        return [r.email, cred?.password ?? "", cred?.totpSecret ?? "", r.status, r.reason || ""].join(":");
      }).join("\n"),
      `gmail_${activeList}.txt`
    );
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
                <MailX className={cn("w-5 h-5", notOpenedAll.length > 0 ? "text-red-400" : "text-muted-foreground/30")} />
                <span className={cn("text-2xl font-mono font-bold", notOpenedAll.length > 0 ? "text-red-400" : "text-muted-foreground/30")}>{notOpenedAll.length}</span>
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
                onClick={() => { setActiveList("opened"); setNotOpenedFilter("all"); }}
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
                NOT OPENED ({notOpenedAll.length})
              </button>
            </div>
            <Button variant="outline" size="sm" onClick={handleExport}
              disabled={displayed.length === 0} className="font-mono text-xs h-8">
              <Download className="w-3 h-3 mr-2" />EXPORT .TXT
            </Button>
          </div>
          {/* NOT OPENED sub-filters — breakdown by failure type */}
          {activeList === "not_opened" && notOpenedAll.length > 0 && (
            <div className="border-b border-border px-3 py-2 flex flex-wrap gap-1 bg-muted/20">
              {([
                { key: "all" as const, label: "ALL" },
                { key: "wrong_password" as const, label: "BAD PASS" },
                { key: "verification_required" as const, label: "VERIFY" },
                { key: "app_password_required" as const, label: "APP PWD" },
                { key: "unknown" as const, label: "UNKNOWN" },
              ]).filter(f => f.key === "all" || notOpenedAll.some(r => r.status === f.key)).map(f => {
                const count = f.key === "all" ? notOpenedAll.length : notOpenedAll.filter(r => r.status === f.key).length;
                return (
                  <button key={f.key} onClick={() => setNotOpenedFilter(f.key)}
                    className={cn("px-2 py-1 text-[10px] font-mono rounded border transition-colors",
                      notOpenedFilter === f.key
                        ? "bg-secondary text-secondary-foreground border-border"
                        : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50"
                    )}>
                    {f.label} ({count})
                  </button>
                );
              })}
            </div>
          )}

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
function BrowserChecker({ action = "check" }: {
  action?: "check" | "create_app_password";
}) {
  const isAppPasswordAction = action === "create_app_password";
  // ── localStorage keys ────────────────────────────────────────────────────
  // App Password runs have their own recoverable job state. Their transient
  // generated passwords are explicitly excluded from the saved result data.
  const storagePrefix = isAppPasswordAction ? "vbc_app_" : "vbc_";
  const LS = {
    input: `${storagePrefix}input`, proxy: `${storagePrefix}proxy`, concurrency: `${storagePrefix}conc`,
    fresh: `${storagePrefix}fresh`, results: `${storagePrefix}results`, total: `${storagePrefix}total`,
    active: `${storagePrefix}active`, savedAt: `${storagePrefix}saved_at`, jobId: `${storagePrefix}job_id`,
    creds: `${storagePrefix}creds`, debugLogs: `${storagePrefix}debug_logs`,
    appPasswordLabel: `${storagePrefix}label`,
    appPasswords: `${storagePrefix}app_passwords`,
  } as const;
  const lsGet = <T,>(key: string, fb: T): T => {
    try { const v = localStorage.getItem(key); return v != null ? JSON.parse(v) : fb; } catch { return fb; }
  };

  const [inputText,    setInputText]    = useState<string>(() => lsGet(LS.input, ""));
  const [proxyText,    setProxyText]    = useState<string>(() => lsGet(LS.proxy, ""));
  const [concurrency,  setConcurrency]  = useState<number>(() => lsGet(LS.concurrency, 3));
  const [freshProfile, setFreshProfile] = useState<boolean>(() => lsGet(LS.fresh, false));
  const [activeList,   setActiveList]   = useState<LoginList>(() => lsGet(LS.active, "opened"));
  const [selectedUnknown, setSelectedUnknown] = useState<Set<string>>(new Set());
  const [total,        setTotal]        = useState<number>(() => lsGet(LS.total, 0));
  const [jobId,        setJobId]        = useState<string | null>(() => localStorage.getItem(LS.jobId));
  const [isRunning,    setIsRunning]    = useState(false);
  const [jobStatus,    setJobStatus]    = useState<string>("idle");
  const [pauseRequested, setPauseRequested] = useState(false);
  const [resumeReady,   setResumeReady] = useState(false);
  const [connStatus,   setConnStatus]   = useState<"idle"|"connecting"|"connected"|"reconnecting"|"disconnected">("idle");
  const [reconnectedAt, setReconnectedAt] = useState<string | null>(null);
  const [debugLogs, setDebugLogs] = useState<BrowserDebugEvent[]>(() => lsGet(LS.debugLogs, []));
  const [debugOpen, setDebugOpen] = useState(true);
  const [appPasswordLabel, setAppPasswordLabel] = useState<string>(() =>
    lsGet(LS.appPasswordLabel, "")
  );
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [logsCopied, setLogsCopied] = useState(false);
  const [appPasswordBanner, setAppPasswordBanner] = useState<{ email: string; password: string } | null>(null);
  const [uploadedFileName, setUploadedFileName] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [results, setResults] = useState<ExtBrowserResult[]>(() => {
    const saved = lsGet(LS.results, []) as ExtBrowserResult[];
    const appPasswords = isAppPasswordAction
      ? lsGet(LS.appPasswords, {} as Record<string, string>)
      : {};
    return saved.map(r => ({
      ...r,
      status: r.status === "checking" ? "unknown" as const : r.status,
      reason: r.status === "checking" ? "Tab was closed/refreshed mid-check" : r.reason,
      appPassword: appPasswords[r.email] ?? r.appPassword,
    }));
  });

  const [restoredAt, setRestoredAt] = useState<string | null>(() => {
    const at = localStorage.getItem(LS.savedAt);
    const saved: ExtBrowserResult[] = lsGet(LS.results, []);
    return saved.length > 0 && at ? at : null;
  });

  // Proxy pre-flight check state
  const [proxyCheckState, setProxyCheckState] = useState<"idle"|"checking"|"ok"|"fail">("idle");
  const [proxyExitIp,     setProxyExitIp]     = useState<string>("");
  const [proxyCheckError, setProxyCheckError] = useState<string>("");

  // refs
  const sseAbortRef      = useRef<AbortController | null>(null);
  const reconnectTimer   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pausePollTimer   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeJobIdRef   = useRef<string | null>(jobId);
  /** Survives applyJobState() merges from redacted server snapshots. */
  const liveAppPasswordsRef = useRef<Record<string, string>>(
    isAppPasswordAction ? lsGet(LS.appPasswords, {} as Record<string, string>) : {},
  );
  const appendModeRef    = useRef(false);
  const debugEndRef      = useRef<HTMLDivElement | null>(null);
  const credentialFileRef = useRef<HTMLInputElement | null>(null);
  // connStatusRef mirrors connStatus state — lets event handlers read the current
  // value without stale closure issues (e.g. visibilitychange, which is mounted once).
  const connStatusRef    = useRef<"idle"|"connecting"|"connected"|"reconnecting"|"disconnected">("idle");
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
    if (isAppPasswordAction) {
      try { localStorage.setItem(LS.appPasswordLabel, JSON.stringify(appPasswordLabel)); } catch {}
    }
  }, [LS.appPasswordLabel, appPasswordLabel, isAppPasswordAction]);
  // Keep connStatusRef in sync so event handlers (visibilitychange) read the current value.
  useEffect(() => { connStatusRef.current = connStatus; }, [connStatus]);
  useEffect(() => {
    try {
      const toSave = results
        .filter(r => r.status !== "checking")
        .map(({ appPassword: _appPassword, ...result }) => result);
      if (toSave.length > 0) {
        localStorage.setItem(LS.results, JSON.stringify(toSave));
        localStorage.setItem(LS.savedAt, new Date().toLocaleTimeString());
      }
      if (isAppPasswordAction) {
        const map = { ...liveAppPasswordsRef.current };
        for (const r of results) {
          const pw = r.appPassword ?? map[r.email];
          if (pw) map[r.email] = pw;
        }
        liveAppPasswordsRef.current = map;
        localStorage.setItem(LS.appPasswords, JSON.stringify(map));
      }
    } catch {}
  }, [results, isAppPasswordAction, LS.results, LS.savedAt, LS.appPasswords]);
  useEffect(() => { try { localStorage.setItem(LS.debugLogs, JSON.stringify(debugLogs)); } catch {} }, [debugLogs]);
  useEffect(() => { if (debugOpen) debugEndRef.current?.scrollIntoView({ block: "end" }); }, [debugLogs, debugOpen]);

  // ── On mount: restore from server ────────────────────────────────────────
  useEffect(() => {
    const saved = localStorage.getItem(LS.jobId);
    if (saved) restoreJobFromServer(saved);
    return () => {
      sseAbortRef.current?.abort();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (pausePollTimer.current) clearTimeout(pausePollTimer.current);
    };
  }, []);

  // ── Auto-reconnect on tab visibility change ───────────────────────────────
  // When user returns to tab after 10-15 min, browser throttles setTimeout so
  // the auto-reconnect timer may not have fired. This fires instantly on tab focus.
  // Uses refs (activeJobIdRef, connStatusRef, reconnectTimer) — never stale closures.
  // State setters (setConnStatus, setReconnectedAt, etc.) are stable across renders.
  useEffect(() => {
    const onVisible = async () => {
      if (document.visibilityState !== "visible") return;
      const id = activeJobIdRef.current;
      if (!id) return;
      // Read current status from ref — no stale closure issue
      const status = connStatusRef.current;
      if (status !== "disconnected" && status !== "reconnecting") return;
      // Cancel stale backoff timer and reconnect immediately
      if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
      setConnStatus("connecting");
      try {
        const res = await apiFetch(`/api/jobs/${id}`);
        if (!res.ok) { setConnStatus("disconnected"); return; }
        const { job } = await res.json();
        if (!job) { setConnStatus("disconnected"); return; }
        if (job.status !== "running") {
          // applyJobState and schedulePausedJobPoll only call stable React setters
          // and access refs — safe to call from a mount-time closure.
          applyJobState(job);
          setConnStatus("idle");
          if (job.status === "paused" && (job.checkingEmails ?? []).length > 0) schedulePausedJobPoll(id);
          return;
        }
        setReconnectedAt(new Date().toLocaleTimeString());
        connectToJobStream(id, job.eventsCount ?? 0);
      } catch { setConnStatus("disconnected"); }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  // Mount-once: all mutable values accessed via refs (activeJobIdRef, connStatusRef,
  // reconnectTimer). React setState setters are guaranteed stable. Functions that
  // only call stable setters + refs (applyJobState, schedulePausedJobPoll,
  // connectToJobStream) are safe to close over.
  // eslint-disable-next-line react-hooks/exhaustive-deps
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

  const getActiveCredentials = () => parseCredentials(inputText);

  const applyUploadedCredentials = (credentials: ParsedCredential[], fileName: string) => {
    setInputText(credentialsToInputText(credentials));
    setUploadedFileName(fileName);
    setUploadError(null);
  };

  const handleCredentialFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    try {
      const credentials = await parseCredentialsFromFile(file);
      if (credentials.length === 0) {
        setUploadError("File mein valid credentials nahi mile. Column A=Email, B=Password, C=2FA — header optional");
        setUploadedFileName(null);
        return;
      }
      applyUploadedCredentials(credentials, file.name);
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "File read nahi ho payi");
      setUploadedFileName(null);
    }
  };

  const clearUploadedCredentials = () => {
    setUploadedFileName(null);
    setUploadError(null);
    if (credentialFileRef.current) credentialFileRef.current.value = "";
  };

  const applyJobState = (job: any) => {
    if (Array.isArray(job.debugEvents)) {
      setDebugLogs(job.debugEvents as BrowserDebugEvent[]);
    }
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
        const preservedAppPassword =
          withCreds.appPassword ?? (idx !== -1 ? merged[idx]?.appPassword : undefined)
          ?? liveAppPasswordsRef.current[r.email];
        const mergedResult: ExtBrowserResult = preservedAppPassword
          ? { ...withCreds, appPassword: preservedAppPassword }
          : withCreds;
        if (idx !== -1) merged[idx] = mergedResult; else merged.push(mergedResult);
      }
      for (const email of (job.checkingEmails ?? [])) {
        if (!merged.some(x => x.email === email))
          merged.push({ email, status: "checking" as const, reason: "Browser running…", totpCode: null });
      }
      return merged;
    });
    setTotal(t => Math.max(t, job.total ?? 0));
    setJobStatus(job.status ?? "idle");
    setIsRunning(job.status === "running");
    setResumeReady(
      ["paused", "interrupted", "cancelled"].includes(job.status) &&
      (job.checkingEmails ?? []).length === 0 &&
      (job.results ?? []).length < (job.total ?? 0),
    );
  };

  const restoreJobFromServer = async (id: string) => {
    try {
      const res = await apiFetch(`/api/jobs/${id}`);
      if (!res.ok) { setJobId(null); return; }
      const { job } = await res.json();
      if (!job) { setJobId(null); return; }
      applyJobState(job);
      setRestoredAt(new Date().toLocaleTimeString());
      if (job.status === "running") {
        setReconnectedAt(new Date().toLocaleTimeString());
        connectToJobStream(id, job.eventsCount ?? 0);
      } else if (job.status === "paused" && (job.checkingEmails ?? []).length > 0) {
        schedulePausedJobPoll(id);
      }
    } catch (e) { console.error("[BrowserChecker] restore:", e); }
  };

  const schedulePausedJobPoll = (id: string) => {
    if (pausePollTimer.current) clearTimeout(pausePollTimer.current);
    pausePollTimer.current = setTimeout(async () => {
      try {
        const res = await apiFetch(`/api/jobs/${id}`);
        if (!res.ok) return;
        const { job } = await res.json();
        if (!job) return;
        applyJobState(job);
        if (job.status === "paused" && (job.checkingEmails ?? []).length > 0) {
          schedulePausedJobPoll(id);
        }
      } catch {
        schedulePausedJobPoll(id);
      }
    }, 2000);
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
        const resp = await apiFetch(`/api/jobs/${id}/stream?since=${since}`, { signal: abort.signal });
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
        // Stream closed normally — re-fetch final job state so resumeReady is set
        // correctly for interrupted/paused jobs (e.g. after server restart).
        try {
          const stateRes = await apiFetch(`/api/jobs/${id}`);
          if (stateRes.ok) {
            const { job } = await stateRes.json();
            if (job) applyJobState(job);
          }
        } catch {}
        setConnStatus("idle"); setIsRunning(false);
      } catch (err: any) {
        if (err?.name === "AbortError") return;
        console.error("[BrowserChecker] SSE:", err);
        setConnStatus("disconnected");
        if (activeJobIdRef.current === id) scheduleReconnect(id);
      }
    })();
  };

  const scheduleReconnect = (id: string, attempt = 0) => {
    // Retry with gentle backoff: 3s, 5s, 7s … max 10s. Keeps retrying forever
    // (browser tab throttles setTimeout when inactive — Live button bypasses this).
    const delay = Math.min(3000 + attempt * 2000, 10000);
    reconnectTimer.current = setTimeout(async () => {
      if (activeJobIdRef.current !== id) return;
      setConnStatus("reconnecting");
      try {
        const res = await apiFetch(`/api/jobs/${id}`);
        if (!res.ok) {
          // Server error — keep retrying
          if (activeJobIdRef.current === id) scheduleReconnect(id, attempt + 1);
          return;
        }
        const { job } = await res.json();
        if (!job) { setConnStatus("disconnected"); return; }
        if (job.status !== "running") {
          applyJobState(job);
          setConnStatus("idle");
          setIsRunning(false);
          if (job.status === "paused" && (job.checkingEmails ?? []).length > 0) {
            schedulePausedJobPoll(id);
          }
          return;
        }
        setReconnectedAt(new Date().toLocaleTimeString());
        connectToJobStream(id, job.eventsCount ?? 0);
      } catch {
        // Network error — keep retrying
        if (activeJobIdRef.current === id) scheduleReconnect(id, attempt + 1);
      }
    }, delay);
  };

  // Manual instant reconnect — clears any pending auto-retry timer and connects now.
  const handleLiveReconnect = async () => {
    const currentId = activeJobIdRef.current ?? jobId;
    if (!currentId) return;
    if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
    setConnStatus("connecting");
    try {
      const res = await apiFetch(`/api/jobs/${currentId}`);
      if (!res.ok) { setConnStatus("disconnected"); return; }
      const { job } = await res.json();
      if (!job) { setConnStatus("disconnected"); return; }
      if (job.status !== "running") {
        applyJobState(job);
        setConnStatus("idle");
        if (job.status === "paused" && (job.checkingEmails ?? []).length > 0) schedulePausedJobPoll(currentId);
        return;
      }
      setReconnectedAt(new Date().toLocaleTimeString());
      connectToJobStream(currentId, job.eventsCount ?? 0);
    } catch { setConnStatus("disconnected"); }
  };

  const handleJobEvent = (evt: any) => {
    if (evt.type === "debug") {
      setDebugLogs(prev => {
        const next = [...prev, evt as BrowserDebugEvent];
        return next.length > 1500 ? next.slice(-1500) : next;
      });
    } else if (evt.type === "checking") {
      setResults(prev => prev.some(r => r.email === evt.email) ? prev :
        [...prev, { email: evt.email, status: "checking" as const, reason: "Browser running…", totpCode: null }]);
    } else if (evt.type === "result") {
      const { type: _t, ...result } = evt;
      const cred = credsMapRef.current[result.email];
      const preservedAppPassword =
        result.appPassword
        ?? liveAppPasswordsRef.current[result.email];
      const withCreds: ExtBrowserResult = {
        ...result,
        password:   cred?.password,
        totpSecret: cred?.totpSecret,
        ...(preservedAppPassword ? { appPassword: preservedAppPassword } : {}),
      };
      if (withCreds.appPassword) {
        liveAppPasswordsRef.current[result.email] = withCreds.appPassword;
        setAppPasswordBanner({ email: result.email, password: withCreds.appPassword });
        setActiveList("opened");
      }
      setResults(prev => {
        const idx = prev.findIndex(r => r.email === result.email);
        const existingPassword = idx !== -1 ? prev[idx]?.appPassword : undefined;
        const mergedPassword =
          withCreds.appPassword
          ?? existingPassword
          ?? liveAppPasswordsRef.current[result.email];
        const mergedResult: ExtBrowserResult = mergedPassword
          ? { ...withCreds, appPassword: mergedPassword }
          : withCreds;
        if (idx !== -1) { const n = [...prev]; n[idx] = mergedResult; return n; }
        return [...prev, mergedResult];
      });
    } else if (evt.type === "paused") {
      setJobStatus("paused");
      setPauseRequested(true);
    } else if (evt.type === "paused_done") {
      setJobStatus("paused");
      setPauseRequested(false);
      setResumeReady(true);
      setIsRunning(false);
      setConnStatus("idle");
    } else if (evt.type === "resumed") {
      setJobStatus("running");
      setPauseRequested(false);
      setResumeReady(false);
      setIsRunning(true);
    } else if (evt.type === "done" || evt.type === "cancelled") {
      setJobStatus(evt.type);
      setPauseRequested(false);
      setResumeReady(false);
      setIsRunning(false); setConnStatus("idle");
    } else if (evt.type === "interrupted") {
      // Server restarted mid-job — partial results saved, resume is possible.
      // Do NOT force resumeReady(false); applyJobState already set it correctly.
      setJobStatus("interrupted");
      setPauseRequested(false);
      setIsRunning(false); setConnStatus("idle");
    }
  };

  const buildBody = (creds: Array<{ email: string; password: string; totp?: string }>) => {
    const proxies = parseProxies(proxyText);
    return JSON.stringify({
      credentials: creds,
      ...(proxies.length > 1 ? { proxies } : proxies.length === 1 ? { proxy: proxies[0] } : {}),
      concurrency, freshProfile,
      action,
      ...(isAppPasswordAction && appPasswordLabel.trim()
        ? { appPasswordLabel: appPasswordLabel.trim().slice(0, 50) }
        : {}),
    });
  };

  const runProxyCheck = async (): Promise<boolean> => {
    const proxies = parseProxies(proxyText);
    if (proxies.length === 0) {
      setProxyCheckState("idle");
      setProxyExitIp("");
      setProxyCheckError("");
      return true;
    }

    setProxyCheckState("checking");
    setProxyExitIp("");
    setProxyCheckError("");

    const checkOneProxy = async (proxy: string, index: number) => {
      try {
        const res = await apiFetch("/api/proxy/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proxy }),
        });
        const data = (await res.json()) as {
          ok: boolean;
          ip?: string;
          error?: string;
          hosting?: boolean;
          mobile?: boolean;
          isp?: string;
          attempts?: number;
        };
        if (!res.ok && !data.error) {
          return { idx: index + 1, ok: false as const, error: `API HTTP ${res.status}` };
        }
        return { idx: index + 1, ...data };
      } catch {
        const apiUp = await isApiReachable();
        return {
          idx: index + 1,
          ok: false as const,
          error: apiUp
            ? "Proxy check request fail — dobara try karo"
            : "API server band hai — Replit mein 'API Server' workflow START karo (port 8080). Tumhara proxy shayad theek hai!",
        };
      }
    };

    const proxyResults = await Promise.all(proxies.map((p, i) => checkOneProxy(p, i)));
    const failed = proxyResults.filter((r) => !r.ok);
    if (failed.length > 0) {
      setProxyCheckState("fail");
      setProxyCheckError(failed.map((f) => `Proxy ${f.idx}: ${f.error ?? "failed"}`).join(" | "));
      return false;
    }

    setProxyCheckState("ok");
    const first = proxyResults[0];
    const attemptNote = first?.attempts && first.attempts > 1
      ? ` (${first.attempts} attempts — datacenter IP skip karke residential mila)`
      : "";
    setProxyExitIp(`${first?.ip ?? ""}${attemptNote}`);
    return true;
  };

  const handleTestProxy = async () => {
    if (parseProxies(proxyText).length === 0) return;
    await runProxyCheck();
  };

  const handleCheck = async () => {
    const credentials = getActiveCredentials();
    if (credentials.length === 0) return;

    const proxies = parseProxies(proxyText);
    if (proxies.length === 0) {
      setProxyCheckState("fail");
      setProxyCheckError("Proxy required — bina proxy Gmail check nahi hoga (Replit datacenter IP se account disable ho sakta hai)");
      return;
    }

    const proxyOk = await runProxyCheck();
    if (!proxyOk) return;

    // Build and persist credential map so exports survive page refresh
    const newCredsMap: Record<string, { password: string; totpSecret?: string }> = {};
    for (const c of credentials) newCredsMap[c.email] = { password: c.password, totpSecret: c.totp };
    credsMapRef.current = newCredsMap;
    try { localStorage.setItem(LS.creds, JSON.stringify(newCredsMap)); } catch {}
    sseAbortRef.current?.abort();
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    appendModeRef.current = false;
    setResults([]); setDebugLogs([]); setTotal(credentials.length); setReconnectedAt(null); setRestoredAt(null); setSelectedUnknown(new Set());
    setJobStatus("running"); setPauseRequested(false); setResumeReady(false);
    try {
      const res = await apiFetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: buildBody(credentials) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { jobId: id } = await res.json();
      setJobId(id); activeJobIdRef.current = id; setIsRunning(true);
      connectToJobStream(id, 0);
    } catch (e) { console.error("[BrowserChecker] handleCheck:", e); }
  };

  const handlePause = async () => {
    const currentId = activeJobIdRef.current ?? jobId;
    if (!currentId) return;
    setPauseRequested(true);
    try {
      const res = await apiFetch(`/api/jobs/${currentId}/pause`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      console.error("[BrowserChecker] pause:", e);
      setPauseRequested(false);
    }
  };

  const handleResume = async () => {
    const currentId = activeJobIdRef.current ?? jobId;
    if (!currentId || !resumeReady) return;
    setResumeReady(false);
    try {
      const res = await apiFetch(`/api/jobs/${currentId}/resume`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setJobStatus("running");
      setIsRunning(true);
      // The previous paused stream has closed. Reconnect from the current
      // server event count so only new resume events are consumed.
      const stateRes = await apiFetch(`/api/jobs/${currentId}`);
      const { job } = await stateRes.json();
      connectToJobStream(currentId, job?.eventsCount ?? 0);
    } catch (e) {
      console.error("[BrowserChecker] resume:", e);
      setResumeReady(true);
      setIsRunning(false);
    }
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
      try { await apiFetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }); } catch {}
    }

    // 3. Wipe all LS keys — input, proxy, config, results, session — everything
    Object.values(LS).forEach(k => { try { localStorage.removeItem(k); } catch {} });
    try { sessionStorage.clear(); } catch {}

    // 4. Clear all refs
    credsMapRef.current = {};
    appendModeRef.current = false;

    // 5. Reset ALL React state — identical to a fresh page load
    setResults([]);
    setDebugLogs([]);
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
    setFreshProfile(false);
  };

  const startRetryJob = async (creds: Array<{ email: string; password: string; totp?: string }>) => {
    sseAbortRef.current?.abort();
    if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    appendModeRef.current = true;
    setTotal(t => t + creds.length);
    try {
      const res = await apiFetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: buildBody(creds) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { jobId: id } = await res.json();
      // Don't call setJobId — keep the original jobId in localStorage so a
      // page refresh restores the original job, not the retry job.
      activeJobIdRef.current = id; setJobStatus("running"); setIsRunning(true);
      setPauseRequested(false); setResumeReady(false);
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
  const unknownList     = results.filter(r => getBrowserResultCategory(r) === "unknown" && r.status !== "checking");
  const fingerprintList = results.filter(r => r.status !== "checking" && !!(r as any).fingerprintData);
  const unknownRetryCount = unknownList.filter(r => !!credsMapRef.current[r.email]).length;
  const completedCount = results.filter(r => r.status !== "checking").length;
  const displayed = activeList === "opened" ? opened
    : activeList === "not_opened" ? notOpened
    : activeList === "delete" ? deleteList
    : activeList === "fingerprint" ? fingerprintList
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

  const resolveAppPassword = (result: ExtBrowserResult) =>
    result.appPassword ?? liveAppPasswordsRef.current[result.email];

  const handleCopyField = async (
    value: string,
    copyKey: string,
    formatter?: (v: string) => string,
  ) => {
    try {
      await navigator.clipboard.writeText(formatter ? formatter(value) : value);
      setCopiedField(copyKey);
      window.setTimeout(() => setCopiedField(current => current === copyKey ? null : current), 1600);
    } catch {
      setCopiedField(null);
    }
  };

  const copyAppPassword = async (email: string, password: string) => {
    await handleCopyField(password, `${email}:appPassword`, formatAppPasswordDisplay);
  };

  const formatDebugLogsForCopy = (logs: BrowserDebugEvent[]) =>
    logs.map(log => {
      const time = new Date(log.timestamp).toLocaleTimeString([], { hour12: false });
      const details = [
        log.step ? `step=${log.step}` : null,
        log.sessionId ? `sticky=${log.sessionId}` : null,
        log.proxyUsername ? `proxy_user=${log.proxyUsername}` : null,
        log.proxyEndpoint ? `endpoint=${log.proxyEndpoint}` : null,
        log.ip ? `ip=${log.ip}` : null,
        log.previousIp ? `previous=${log.previousIp}` : null,
        log.retry != null ? `retries=${log.retry}${log.retries != null ? `/${log.retries}` : ""}` : null,
        log.durationMs != null ? `step_time=${(log.durationMs / 1000).toFixed(1)}s` : null,
        log.totalDurationMs != null ? `total=${(log.totalDurationMs / 1000).toFixed(1)}s` : null,
      ].filter(Boolean);
      const lines = [
        time,
        log.category,
        `[${log.account}] ${log.message}`,
      ];
      if (details.length > 0) lines.push(details.join(" · "));
      if (log.url) lines.push(`url=${log.url}`);
      return lines.join("\n");
    }).join("\n\n");

  const copyAllDebugLogs = async () => {
    if (debugLogs.length === 0) return;
    try {
      await navigator.clipboard.writeText(formatDebugLogsForCopy(debugLogs));
      setLogsCopied(true);
      window.setTimeout(() => setLogsCopied(false), 1600);
    } catch {
      setLogsCopied(false);
    }
  };

  const exportRows = (rows: ExtBrowserResult[]) =>
    rows.filter(r => r.status !== "checking").map(r => ({
      username: r.email,
      password: r.password ?? "",
      totpSecret: r.totpSecret ?? "",
      appPassword: resolveAppPassword(r) ? formatAppPasswordDisplay(resolveAppPassword(r)!) : "",
      result: statusLabel(r.status),
    }));

  const downloadAppPasswords = () => {
    const rows = exportRows(results).filter(r => r.appPassword);
    download(
      rows.map(r => `${r.username}:${r.password}:${r.totpSecret}:${r.appPassword}`).join("\n"),
      "google_app_passwords.txt",
      "text/plain",
    );
  };

  const downloadCSV = (rows: ExtBrowserResult[], name: string) => {
    const csv = (s: string) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    const shaped = exportRows(rows);
    const header = isAppPasswordAction
      ? "Username,Password,2FA Secret,App Password,Result"
      : "Email,Password,2FA Secret,Result";
    const body = shaped.map(r => (
      isAppPasswordAction
        ? [csv(r.username), csv(r.password), csv(r.totpSecret), csv(r.appPassword), csv(r.result)]
        : [csv(r.username), csv(r.password), csv(r.totpSecret), csv(r.result)]
    ).join(",")).join("\n");
    download("\uFEFF" + header + "\n" + body, name + ".csv", "text/csv;charset=utf-8");
  };

  const downloadJSON = (rows: ExtBrowserResult[], name: string) => {
    const shaped = exportRows(rows).map(r => (
      isAppPasswordAction
        ? {
            username: r.username,
            password: r.password,
            twoFactorSecret: r.totpSecret,
            appPassword: r.appPassword,
            result: r.result,
          }
        : {
            email: r.username,
            password: r.password,
            twoFactorSecret: r.totpSecret,
            result: r.result,
          }
    ));
    download(JSON.stringify(shaped, null, 2), name + ".json", "application/json");
  };

  const downloadExcel = (rows: ExtBrowserResult[], name: string) => {
    const shaped = exportRows(rows);
    const headers = isAppPasswordAction
      ? ["Username", "Password", "2FA Secret", "App Password", "Result"]
      : ["Email", "Password", "2FA Secret", "Result"];
    const data = shaped.map(r => (
      isAppPasswordAction
        ? [r.username, r.password, r.totpSecret, r.appPassword, r.result]
        : [r.username, r.password, r.totpSecret, r.result]
    ));
    const worksheet = XLSX.utils.aoa_to_sheet([headers, ...data]);
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, worksheet, "Results");
    const buffer = XLSX.write(workbook, { bookType: "xlsx", type: "array" });
    downloadBlob(
      new Blob([buffer], {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
      `${name}.xlsx`,
    );
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
    <div className="space-y-4">
      {isAppPasswordAction && appPasswordBanner && (
        <div className="rounded-lg border border-green-500/50 bg-green-500/10 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-mono uppercase tracking-wider text-green-300/80">App password created</p>
            <p className="text-sm text-green-100/90 truncate">{appPasswordBanner.email}</p>
            <p className="font-mono text-lg font-bold tracking-[0.35em] text-green-300 mt-1">
              {formatAppPasswordDisplay(appPasswordBanner.password)}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="font-mono text-xs border-green-500/40 text-green-300 hover:bg-green-500/15"
              onClick={() => copyAppPassword(appPasswordBanner.email, appPasswordBanner.password)}
            >
              <Copy className="w-3.5 h-3.5 mr-1" />
              {copiedField === `${appPasswordBanner.email}:appPassword` ? "COPIED" : "COPY PASSWORD"}
            </Button>
            <button
              type="button"
              onClick={() => setAppPasswordBanner(null)}
              className="text-[10px] font-mono px-2 py-1 rounded border border-green-500/25 text-green-200/70 hover:bg-green-500/10"
            >
              DISMISS
            </button>
          </div>
        </div>
      )}
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Input */}
      <div className="lg:col-span-1 space-y-3">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              {isAppPasswordAction ? (
                <><KeyRound className="w-3.5 h-3.5 text-primary" />Create Google App Password</>
              ) : (
                <><Globe className="w-3.5 h-3.5 text-primary" />Browser Login Check</>
              )}
              {connStatus !== "idle" && (
                <span className={cn("ml-auto flex items-center gap-1.5")}>
                  <span className={cn("text-[10px] font-mono", connLabel[connStatus].cls)}>
                    {connLabel[connStatus].text}
                  </span>
                  {(connStatus === "disconnected" || connStatus === "reconnecting") && (
                    <button
                      onClick={handleLiveReconnect}
                      title="Turant live ho jao"
                      className="flex items-center gap-0.5 text-[10px] font-mono px-1.5 py-0.5 rounded border border-blue-500/50 text-blue-400 hover:bg-blue-500/15 hover:border-blue-500/70 transition-colors">
                      <Zap className="w-2.5 h-2.5" />LIVE
                    </button>
                  )}
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <label className="text-[10px] font-mono uppercase tracking-widest text-muted-foreground">
                  Credentials (paste ya file upload)
                </label>
                <div className="flex items-center gap-2">
                  <input
                    ref={credentialFileRef}
                    type="file"
                    accept=".xlsx,.xls,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel,text/csv"
                    className="hidden"
                    onChange={handleCredentialFileUpload}
                    disabled={isChecking}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="font-mono text-[10px] h-7 px-2"
                    disabled={isChecking}
                    onClick={() => credentialFileRef.current?.click()}
                  >
                    <Upload className="w-3 h-3 mr-1" />
                    UPLOAD EXCEL / CSV
                  </Button>
                  {uploadedFileName && (
                    <button
                      type="button"
                      onClick={clearUploadedCredentials}
                      className="text-[10px] font-mono px-2 py-1 rounded border border-border text-muted-foreground hover:text-foreground"
                      disabled={isChecking}
                    >
                      CLEAR FILE
                    </button>
                  )}
                </div>
              </div>
              {uploadedFileName && (
                <p className="text-[10px] font-mono text-green-400/80">
                  Loaded {getActiveCredentials().length} account(s) from {uploadedFileName}
                </p>
              )}
              {uploadError && (
                <p className="text-[10px] font-mono text-red-400/80">{uploadError}</p>
              )}
              <p className="text-[10px] font-mono text-muted-foreground/70">
                File: Column A = Email, B = Password, C = 2FA (header row optional)
                {" "}— ya neeche <span className="text-foreground/80">email:pass:2fa</span> paste karo.
              </p>
            </div>
            <Textarea
              placeholder={"email@gmail.com:password\nemail2@gmail.com:password:2FA_SECRET"}
              className="min-h-[180px] resize-y bg-background/50 font-mono text-sm leading-relaxed"
              value={inputText}
              onChange={e => { setInputText(e.target.value); setUploadError(null); }}
              disabled={isChecking}
            />

            {/* Proxy */}
            {(() => {
              const proxies = parseProxies(proxyText);
              const count = proxies.length;
              const creds = getActiveCredentials().length;
              return (
                <div className={cn(
                  "rounded-lg border p-2.5 text-[11px] font-mono space-y-1",
                  count > 0
                    ? "border-green-500/40 bg-green-500/5 text-green-400/90"
                    : "border-yellow-500/40 bg-yellow-500/5 text-yellow-400/90"
                )}>
                  {count === 0 ? (
                    <><p className="font-semibold">⚠ Residential proxy zaroori hai</p>
                      <p className="text-yellow-300/70">Bina proxy check start nahi hoga — Replit datacenter IP se Gmail login account disable kar deta hai. Sirf apna proxy URL daalo.</p></>
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
                  proxyCheckState === "fail" ? "border-red-500/60" :
                  proxyCheckState === "ok"   ? "border-green-500/60" :
                  parseProxies(proxyText).length > 0 ? "border-green-500/50" : "border-yellow-500/40"
                )}
                value={proxyText}
                onChange={e => { setProxyText(e.target.value); setProxyCheckState("idle"); setProxyCheckError(""); setProxyExitIp(""); }}
                disabled={isChecking}
              />

              {/* Proxy pre-flight result banner */}
              {proxyCheckState === "checking" && (
                <div className="rounded-md border border-blue-500/30 bg-blue-500/5 px-3 py-2 text-[11px] font-mono text-blue-300 flex items-center gap-2">
                  <span className="animate-spin inline-block">⟳</span>
                  Proxy check chal raha hai… (datacenter IP aaye to auto-retry, 3 min max)
                </div>
              )}
              {proxyCheckState === "ok" && (
                <div className="rounded-md border border-green-500/40 bg-green-500/5 px-3 py-2 text-[11px] font-mono text-green-300 space-y-0.5">
                  <p className="font-semibold">✅ Proxy working — exit IP: <span className="text-green-200">{proxyExitIp}</span></p>
                  <p className="text-green-400/60">Sirf residential proxy IP se Gmail check hoga — datacenter IP aaye to auto-retry (10 tak)</p>
                </div>
              )}
              {proxyCheckState === "fail" && (
                <div className="rounded-md border border-red-500/40 bg-red-500/5 px-3 py-2 text-[11px] font-mono text-red-300 space-y-1">
                  <p className="font-semibold">❌ Proxy check fail</p>
                  <p className="text-red-300/70 break-all">{proxyCheckError}</p>
                  {proxyCheckError.includes("API server band") ? (
                    <p className="text-red-400/50 mt-1">Replit → Workflows → &quot;API Server&quot; run karo. ProxyScrape data (149 MB) alag cheez hai — pehle backend chalu hona zaroori hai.</p>
                  ) : (
                    <p className="text-red-400/50 mt-1">ProxyScrape dashboard se credentials verify karo, phir TEST PROXY dabao.</p>
                  )}
                </div>
              )}
              {parseProxies(proxyText).length > 0 && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="w-full font-mono text-[11px] uppercase tracking-wider"
                  onClick={handleTestProxy}
                  disabled={isChecking || proxyCheckState === "checking"}
                >
                  {proxyCheckState === "checking" ? "Testing proxy…" : "Test proxy"}
                </Button>
              )}
            </div>

            {isAppPasswordAction && (
              <div className="space-y-1.5">
                <label className="text-[10px] font-mono uppercase tracking-widest text-primary/80 flex items-center gap-1">
                  <KeyRound className="w-3 h-3" /> App name (optional override)
                </label>
                <Input
                  placeholder="Random Indian name auto-use hoga"
                  className="bg-background/50 font-mono text-sm"
                  value={appPasswordLabel}
                  onChange={e => setAppPasswordLabel(e.target.value.slice(0, 50))}
                  disabled={isChecking}
                />
                <p className="text-[10px] text-muted-foreground/70 font-mono leading-relaxed">
                  Khali chhodo to har run par random Indian naam use hoga (Arjun, Priya, Rahul, etc.). Custom naam chahiye ho to yahan likho.
                </p>
              </div>
            )}

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
                {freshProfile ? "⚠️ Naya device har run — account 2-3 din mein flag ho sakta hai" : "✓ Same device reuse — account safe, faster second check"}
              </p>
            </button>

            <div className="text-xs text-muted-foreground font-mono bg-muted/30 rounded p-2 border border-border space-y-1">
              <p className="text-foreground/70 font-medium">Format:</p>
              <p><span className="text-primary">email:password</span></p>
              <p><span className="text-primary">email:password:2FA_SECRET</span></p>
              <p className="text-yellow-400/70 mt-1">
                {isAppPasswordAction
                  ? "⚠ ~30–60s per account (login via App Passwords page + create)"
                  : "⚠ ~20–40s per account (real browser)"}
              </p>
            </div>

            {isChecking ? (
              <Button variant="destructive" className="w-full font-mono font-medium tracking-wide" size="lg" onClick={handlePause} disabled={pauseRequested}>
                <PauseCircle className="w-4 h-4 mr-2" />{pauseRequested ? "PAUSING…" : "PAUSE CHECK"}
              </Button>
            ) : resumeReady ? (
              <Button className="w-full font-mono font-medium tracking-wide" size="lg" onClick={handleResume}>
                <PlayCircle className="w-4 h-4 mr-2" />RESUME FROM WHERE STOPPED
              </Button>
            ) : (
              <Button className="w-full font-mono font-medium tracking-wide" size="lg"
                onClick={handleCheck} disabled={getActiveCredentials().length === 0 || parseProxies(proxyText).length === 0}>
                {isAppPasswordAction ? (
                  <><KeyRound className="w-4 h-4 mr-2" />LOGIN & CREATE APP PASSWORD</>
                ) : (
                  <><Globe className="w-4 h-4 mr-2" />OPEN BROWSER & CHECK</>
                )}
              </Button>
            )}
            {/* Live reconnect — instant, bypasses browser throttle on inactive tabs */}
            {(connStatus === "disconnected" || connStatus === "reconnecting") && (
              <Button size="sm" onClick={handleLiveReconnect}
                className="w-full font-mono text-xs h-8 bg-blue-600 hover:bg-blue-500 text-white border-blue-500/50 transition-colors">
                <Zap className="w-3 h-3 mr-1.5" />LIVE — TURANT CONNECT KRO
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
          <div className="grid grid-cols-3 md:grid-cols-5 gap-2">
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "opened" ? "border-green-500/60 bg-green-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("opened")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <MailCheck className={cn("w-4 h-4", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", opened.length > 0 ? "text-green-400" : "text-muted-foreground/30")}>{opened.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">
                  {isAppPasswordAction ? "CREATED" : "OPEN"}
                </span>
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
            <Card className={cn("border-2 cursor-pointer transition-colors", activeList === "fingerprint" ? "border-purple-500/60 bg-purple-500/5" : "border-border bg-card/40")}
              onClick={() => setActiveList("fingerprint")}>
              <CardContent className="p-3 flex flex-col items-center gap-1">
                <ShieldAlert className={cn("w-4 h-4", fingerprintList.length > 0 ? "text-purple-400" : "text-muted-foreground/30")} />
                <span className={cn("text-xl font-mono font-bold", fingerprintList.length > 0 ? "text-purple-400" : "text-muted-foreground/30")}>{fingerprintList.length}</span>
                <span className="text-[9px] font-mono uppercase tracking-widest text-muted-foreground">FINGERPRINT</span>
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
        {jobStatus === "paused" && !isRunning && (
          <div className="flex items-center justify-between rounded-lg border border-yellow-500/30 bg-yellow-500/5 px-3 py-2 text-[11px] font-mono text-yellow-400/80">
            <span>⏸ Check paused — {completedCount}/{total} results saved. Pending accounts will continue from here.</span>
            {resumeReady && <button onClick={handleResume} className="ml-3 text-yellow-300 hover:text-yellow-200 transition-colors">RESUME</button>}
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

        {/* Persisted real-time browser diagnostics */}
        <Card className="border-cyan-500/25 bg-[#071016] overflow-hidden">
          <div className="border-b border-cyan-500/20 px-3 py-2.5 flex items-center justify-between gap-3 bg-cyan-500/[0.035]">
            <button
              type="button"
              onClick={() => setDebugOpen(open => !open)}
              className="flex items-center gap-2 text-left min-w-0"
              aria-expanded={debugOpen}
            >
              <Terminal className="w-4 h-4 text-cyan-300 shrink-0" />
              <span className="font-mono text-xs uppercase tracking-widest text-cyan-100">Debug Logs</span>
              <span className="text-[10px] font-mono text-cyan-300/60">{debugLogs.length} events</span>
              <span className="text-xs text-cyan-300/70">{debugOpen ? "▾" : "▸"}</span>
            </button>
            <div className="flex items-center gap-2 shrink-0">
              {debugLogs.length > 0 && (
                <button
                  type="button"
                  onClick={copyAllDebugLogs}
                  className="text-[10px] font-mono px-2 py-1 rounded border border-cyan-500/25 text-cyan-200/70 hover:bg-cyan-400/10 hover:text-cyan-100 transition-colors"
                >
                  {logsCopied ? "COPIED" : "COPY ALL"}
                </button>
              )}
              {isChecking && <span className="flex items-center gap-1.5 text-[10px] font-mono text-green-300"><span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />LIVE</span>}
              {debugLogs.length > 0 && (
                <button
                  type="button"
                  onClick={() => setDebugLogs([])}
                  className="text-[10px] font-mono px-2 py-1 rounded border border-cyan-500/25 text-cyan-200/70 hover:bg-cyan-400/10 hover:text-cyan-100 transition-colors"
                >
                  CLEAR
                </button>
              )}
            </div>
          </div>
          {debugOpen && (
            <div className="h-[340px] overflow-y-auto overscroll-contain p-3 font-mono text-[11px] leading-relaxed bg-[#03080c]">
              {debugLogs.length === 0 ? (
                <div className="h-full flex flex-col items-center justify-center text-center text-cyan-100/35 gap-2">
                  <Terminal className="w-6 h-6" />
                  <p>Start {isAppPasswordAction ? "App Password creation" : "Browser Check"} to stream proxy, IP, Chrome, and Google flow diagnostics.</p>
                  <p className="text-[10px] text-cyan-100/25">Logs remain here after the run and page refresh.</p>
                </div>
              ) : debugLogs.map((log, index) => {
                const time = new Date(log.timestamp).toLocaleTimeString([], { hour12: false });
                const color = log.level === "error"
                  ? "text-red-300"
                  : log.level === "warn"
                    ? "text-amber-300"
                    : log.category === "step"
                      ? "text-violet-300"
                      : log.category === "network"
                        ? "text-green-300"
                        : log.category === "proxy"
                          ? "text-cyan-300"
                          : "text-slate-200";
                const details = [
                  log.step ? `step=${log.step}` : null,
                  log.sessionId ? `sticky=${log.sessionId}` : null,
                  log.proxyUsername ? `proxy_user=${log.proxyUsername}` : null,
                  log.proxyEndpoint ? `endpoint=${log.proxyEndpoint}` : null,
                  log.ip ? `ip=${log.ip}` : null,
                  log.previousIp ? `previous=${log.previousIp}` : null,
                  log.retry != null ? `retries=${log.retry}${log.retries != null ? `/${log.retries}` : ""}` : null,
                  log.durationMs != null ? `step_time=${(log.durationMs / 1000).toFixed(1)}s` : null,
                  log.totalDurationMs != null ? `total=${(log.totalDurationMs / 1000).toFixed(1)}s` : null,
                ].filter(Boolean);
                return (
                  <div key={`${log.timestamp}-${index}`} className="grid grid-cols-[auto_auto_1fr] gap-x-2 py-1 border-b border-white/[0.035] last:border-0">
                    <span className="text-slate-500 tabular-nums">{time}</span>
                    <span className={cn("uppercase text-[9px] tracking-wider", color)}>{log.category}</span>
                    <div className="min-w-0">
                      <span className="text-slate-400">[{log.account}] </span>
                      <span className={color}>{log.message}</span>
                      {details.length > 0 && <div className="text-[10px] text-slate-500 break-all">{details.join(" · ")}</div>}
                      {log.url && <div className="text-[10px] text-blue-300/75 break-all">url={log.url}</div>}
                    </div>
                  </div>
                );
              })}
              <div ref={debugEndRef} />
            </div>
          )}
        </Card>

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
              <button onClick={() => setActiveList("fingerprint")}
                className={cn("flex items-center gap-2 px-3 py-1.5 rounded-md border text-xs font-mono font-medium transition-colors",
                  activeList === "fingerprint" ? "bg-purple-500/10 text-purple-400 border-purple-500/40" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50")}>
                <ShieldAlert className="w-3.5 h-3.5" />FINGERPRINT ({fingerprintList.length})
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
              <>
                <Button variant="outline" size="sm"
                  onClick={() => {
                    const rows = displayed.filter(r => r.status !== "checking");
                    if (isAppPasswordAction) {
                      download(
                        exportRows(rows).map(r =>
                          [r.username, r.password, r.totpSecret, r.appPassword, r.result].join(":")
                        ).join("\n"),
                        `gmail_${activeList}.txt`,
                        "text/plain",
                      );
                    } else {
                      download(
                        rows.map(r =>
                          [r.email, r.password ?? "", r.totpSecret ?? "", statusLabel(r.status)].join(":")
                        ).join("\n"),
                        `gmail_${activeList}.txt`,
                        "text/plain",
                      );
                    }
                  }}
                  disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                  <Download className="w-3 h-3 mr-1" />.TXT
                </Button>
                <Button variant="outline" size="sm"
                  onClick={() => downloadCSV(displayed.filter(r => r.status !== "checking"), `gmail_${activeList}`)}
                  disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                  <Download className="w-3 h-3 mr-1" />.CSV
                </Button>
                <Button variant="outline" size="sm"
                  onClick={() => downloadExcel(displayed.filter(r => r.status !== "checking"), `gmail_${activeList}`)}
                  disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                  <Download className="w-3 h-3 mr-1" />.XLSX
                </Button>
                <Button variant="outline" size="sm"
                  onClick={() => downloadJSON(displayed.filter(r => r.status !== "checking"), `gmail_${activeList}`)}
                  disabled={displayed.filter(r => r.status !== "checking").length === 0} className="font-mono text-xs h-8 px-2">
                  <Download className="w-3 h-3 mr-1" />.JSON
                </Button>
              </>
            </div>
          </div>

          <div className="flex-1 overflow-auto">
            {activeList === "fingerprint" ? (
              /* ── Fingerprint grid view ── */
              <div className="p-3 space-y-3">
                {fingerprintList.length === 0 ? (
                  <EmptyState icon={<ShieldAlert className="w-8 h-8 mb-3 opacity-50" />} label="NO FINGERPRINT DATA — RUN A CHECK FIRST" />
                ) : fingerprintList.map((r) => {
                  const fd = (r as any).fingerprintData as {
                    model?: string; androidVersion?: string; chromeVersion?: string; platform?: string;
                    screenW?: number; screenH?: number; dpr?: number;
                    webglVendor?: string; webglRenderer?: string;
                    hwConcurrency?: number; deviceMemory?: number; maxTouchPoints?: number;
                    language?: string; timezone?: string; countryCode?: string; geoLocked?: boolean;
                    batteryLevel?: number; batteryCharging?: boolean; dischargingTime?: number;
                    doNotTrack?: string | null; connectionRtt?: number; connectionDownlink?: number;
                    historyLength?: number; canvasSeed?: number; audioNoise?: number; webglNoise?: number;
                  };
                  if (!fd) return null;
                  const bat = fd.batteryLevel != null ? `${Math.round(fd.batteryLevel * 100)}%` : "—";
                  const batIcon = fd.batteryCharging ? "⚡" : "🔋";
                  const dtH = fd.dischargingTime != null
                    ? `${Math.floor(fd.dischargingTime / 3600)}h ${Math.round((fd.dischargingTime % 3600) / 60)}m left`
                    : "—";
                  const dnt = fd.doNotTrack === "1" ? "ON" : fd.doNotTrack === "unspecified" ? "Unspecified" : fd.doNotTrack == null ? "OFF" : String(fd.doNotTrack);
                  return (
                    <div key={r.email} className="rounded-lg border border-border bg-card/60 p-3 space-y-3">
                      {/* Header */}
                      <div className="flex items-center justify-between flex-wrap gap-2">
                        <div className="flex items-center gap-2">
                          <ShieldAlert className="w-3.5 h-3.5 text-purple-400" />
                          <span className="font-mono text-sm text-foreground font-medium">{r.email}</span>
                          {r.durationMs != null && (
                            <span className={cn("text-[10px] font-mono", r.durationMs < 60000 ? "text-green-400/70" : "text-yellow-400/70")}>
                              {Math.round(r.durationMs / 1000)}s
                            </span>
                          )}
                        </div>
                        <BrowserStatusBadge status={r.status} />
                      </div>

                      {/* 6-section grid */}
                      <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[10px] font-mono">

                        {/* Device Identity */}
                        <div className="rounded border border-purple-500/20 bg-purple-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-purple-400/70 font-bold mb-1">📱 Device</p>
                          <p className="text-purple-300 font-bold text-[11px]">{fd.model ?? "—"}</p>
                          <p className="text-purple-400/80">Android <span className="text-purple-300">{fd.androidVersion ?? "—"}</span></p>
                          <p className="text-purple-400/80">Chrome <span className="text-purple-300">{fd.chromeVersion ?? "—"}</span></p>
                          <p className="text-purple-400/50 text-[9px] break-words">{fd.platform ?? "—"}</p>
                        </div>

                        {/* Screen + GPU */}
                        <div className="rounded border border-cyan-500/20 bg-cyan-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-cyan-400/70 font-bold mb-1">🖥 Screen / GPU</p>
                          <p className="text-cyan-300 font-bold">{fd.screenW ?? "—"}×{fd.screenH ?? "—"}</p>
                          <p className="text-cyan-400/80">DPR <span className="text-cyan-300">{fd.dpr ?? "—"}</span></p>
                          <p className="text-cyan-400/60 text-[9px] break-words mt-1">{fd.webglVendor ?? "—"}</p>
                          <p className="text-cyan-400/50 text-[9px] break-words">{fd.webglRenderer ?? "—"}</p>
                        </div>

                        {/* Hardware */}
                        <div className="rounded border border-green-500/20 bg-green-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-green-400/70 font-bold mb-1">⚙️ Hardware</p>
                          <p className="text-green-400/80">CPU <span className="text-green-300">{fd.hwConcurrency ?? "—"} cores</span></p>
                          <p className="text-green-400/80">RAM <span className="text-green-300">{fd.deviceMemory ?? "—"} GB</span></p>
                          <p className="text-green-400/80">Touch <span className="text-green-300">{fd.maxTouchPoints ?? "—"} pts</span></p>
                        </div>

                        {/* Locale / Geo */}
                        <div className="rounded border border-yellow-500/20 bg-yellow-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-yellow-400/70 font-bold mb-1">🌐 Locale</p>
                          <p className="text-yellow-400/70">Lang <span className="text-yellow-300">{fd.language ?? "—"}</span></p>
                          <p className="text-yellow-400/70">TZ <span className="text-yellow-300 break-words">{fd.timezone ?? "—"}</span></p>
                          <p className="text-yellow-400/70">CC <span className="text-yellow-300">{fd.countryCode ?? "—"}</span></p>
                          <p className={cn("text-[9px] mt-1", fd.geoLocked ? "text-green-400/80" : "text-yellow-400/50")}>
                            {fd.geoLocked ? "🔒 Geo-locked" : "⚠ Not geo-locked"}
                          </p>
                        </div>

                        {/* Battery */}
                        <div className="rounded border border-orange-500/20 bg-orange-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-orange-400/70 font-bold mb-1">🔋 Battery</p>
                          <p className="text-orange-300 font-bold">{bat} {batIcon}</p>
                          <p className="text-orange-400/70">{fd.batteryCharging ? "Charging" : "Discharging"}</p>
                          <p className="text-orange-400/60 text-[9px]">{dtH}</p>
                        </div>

                        {/* Connection + Browser */}
                        <div className="rounded border border-blue-500/20 bg-blue-500/5 p-2 space-y-1">
                          <p className="text-[9px] uppercase tracking-widest text-blue-400/70 font-bold mb-1">📶 Connection</p>
                          <p className="text-blue-400/80">DL <span className="text-blue-300">{fd.connectionDownlink ?? "—"} Mbps</span></p>
                          <p className="text-blue-400/80">RTT <span className="text-blue-300">{fd.connectionRtt ?? "—"} ms</span></p>
                          <p className="text-blue-400/80 mt-1">History <span className="text-blue-300">{fd.historyLength ?? "—"}</span></p>
                          <p className="text-blue-400/80">DNT <span className="text-blue-300">{dnt}</span></p>
                        </div>

                      </div>

                      {/* Noise seeds footer */}
                      <div className="flex flex-wrap gap-3 text-[9px] font-mono text-muted-foreground/40 border-t border-border/40 pt-2">
                        <span>canvas_seed=<span className="text-muted-foreground/60">{fd.canvasSeed ?? "—"}</span></span>
                        <span>audio_noise=<span className="text-muted-foreground/60">{fd.audioNoise ?? "—"}</span></span>
                        <span>webgl_noise=<span className="text-muted-foreground/60">{fd.webglNoise ?? "—"}</span></span>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : results.length === 0 && !isChecking ? (
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
                    <TableHead className="font-mono text-xs">{isAppPasswordAction ? "USERNAME" : "EMAIL"}</TableHead>
                    <TableHead className="font-mono text-xs min-w-[110px]">PASSWORD</TableHead>
                    <TableHead className="font-mono text-xs min-w-[140px]">2FA SECRET</TableHead>
                    <TableHead className="font-mono text-xs w-[130px]">RESULT</TableHead>
                    {isAppPasswordAction && (
                      <TableHead className="font-mono text-xs min-w-[180px]">APP PASSWORD</TableHead>
                    )}
                    <TableHead className="font-mono text-xs min-w-[160px]">REASON</TableHead>
                    {displayed.some(r => r.durationMs != null) && (
                      <TableHead className="font-mono text-xs w-[65px]">TIME</TableHead>
                    )}
                    {displayed.some(r => (r as any).proxySession) && (
                      <TableHead className="font-mono text-xs w-[130px]">PROXY SESSION</TableHead>
                    )}
                    {displayed.some(r => (r as any).ipInfo) && (
                      <TableHead className="font-mono text-xs min-w-[180px]">EXIT IP</TableHead>
                    )}
                    {displayed.some(r => (r as any).fingerprint || (r as any).fingerprintData) && (
                      <TableHead className="font-mono text-xs min-w-[220px]">FINGERPRINT</TableHead>
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
                      <TableCell className="font-medium text-foreground/90">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="truncate">{r.email}</span>
                          <CopyIconButton
                            value={r.email}
                            copyKey={`${r.email}:email`}
                            copiedKey={copiedField}
                            onCopy={handleCopyField}
                          />
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground max-w-[150px]">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="truncate" title={r.password ?? ""}>
                            {r.password ?? <span className="opacity-30">—</span>}
                          </span>
                          <CopyIconButton
                            value={r.password}
                            copyKey={`${r.email}:password`}
                            copiedKey={copiedField}
                            onCopy={handleCopyField}
                          />
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-cyan-400/80 max-w-[160px]">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="truncate" title={r.totpSecret ?? ""}>
                            {r.totpSecret ?? <span className="text-muted-foreground opacity-30">—</span>}
                          </span>
                          <CopyIconButton
                            value={r.totpSecret}
                            copyKey={`${r.email}:totp`}
                            copiedKey={copiedField}
                            onCopy={handleCopyField}
                          />
                        </div>
                      </TableCell>
                      <TableCell><BrowserStatusBadge status={r.status} /></TableCell>
                      {isAppPasswordAction && (
                        <TableCell className="font-mono text-xs">
                          {resolveAppPassword(r) ? (
                            <div className="flex items-center gap-1.5 min-w-0">
                              <span className="text-green-400 font-bold tracking-widest truncate">
                                {formatAppPasswordDisplay(resolveAppPassword(r)!)}
                              </span>
                              <CopyIconButton
                                value={resolveAppPassword(r)}
                                copyKey={`${r.email}:appPassword`}
                                copiedKey={copiedField}
                                onCopy={(value, key) => handleCopyField(value, key, formatAppPasswordDisplay)}
                                className="border-green-500/40 text-green-400 hover:bg-green-500/10"
                              />
                            </div>
                          ) : (
                            <span className="text-muted-foreground opacity-30">—</span>
                          )}
                        </TableCell>
                      )}
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
                      {displayed.some(x => (x as any).ipInfo) && (
                        <TableCell className="text-[10px] font-mono leading-relaxed">
                          {(r as any).ipInfo ? (() => {
                            const ip = (r as any).ipInfo;
                            const utcOffset = typeof ip.offset === "number" ? `UTC${ip.offset >= 0 ? "+" : ""}${ip.offset / 3600}` : null;
                            return (
                              <div className="space-y-0.5 text-[10px] font-mono">
                                {/* IP + type badges */}
                                <div className="flex items-center gap-1 flex-wrap">
                                  <span className="text-green-400/90 font-bold tracking-wider">{ip.ip ?? "—"}</span>
                                  {ip.mobile === true && <span className="px-1 py-0.5 rounded bg-green-500/20 text-green-400">📱 MOBILE</span>}
                                  {ip.proxy === true && <span className="px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-400">🔀 PROXY</span>}
                                  {ip.hosting === true && <span className="px-1 py-0.5 rounded bg-red-500/20 text-red-400">🖥 DC</span>}
                                </div>
                                {/* Location */}
                                <div className="text-muted-foreground/80">{[ip.district, ip.city, ip.zip, ip.region, ip.country].filter(Boolean).join(", ")}</div>
                                {/* Continent + currency + UTC */}
                                <div className="text-muted-foreground/60">{[ip.continent, ip.currency, utcOffset].filter(Boolean).join(" · ")}</div>
                                {/* ISP / Org */}
                                <div className="text-blue-400/70 break-words">{ip.isp ?? ""}</div>
                                {ip.org && ip.org !== ip.isp && <div className="text-cyan-400/60 break-words">{ip.org}</div>}
                                {/* AS */}
                                {ip.asname && <div className="text-purple-400/60 break-words">{ip.asname}</div>}
                                {/* Reverse DNS */}
                                {ip.reverse && <div className="text-muted-foreground/50 break-all">{ip.reverse}</div>}
                              </div>
                            );
                          })() : "—"}
                        </TableCell>
                      )}
                      {displayed.some(x => (x as any).fingerprint || (x as any).fingerprintData) && (
                        <TableCell className="text-[10px] font-mono leading-relaxed min-w-[220px]">
                          {(() => {
                            const fd = (r as any).fingerprintData;
                            if (!fd) return <span className="text-muted-foreground">{(r as any).fingerprint ?? "—"}</span>;
                            const bat = fd.batteryLevel != null ? `${Math.round(fd.batteryLevel * 100)}%${fd.batteryCharging ? "⚡" : "🔋"}` : null;
                            const dtH = fd.dischargingTime != null ? `${Math.round(fd.dischargingTime / 60)}min` : null;
                            const dnt = fd.doNotTrack === "1" ? "on" : fd.doNotTrack === "unspecified" ? "unspec" : fd.doNotTrack == null ? "off" : fd.doNotTrack;
                            return (
                              <div className="space-y-1">
                                {/* Device */}
                                <div className="text-purple-300 font-bold">{fd.model ?? "—"}</div>
                                <div className="text-purple-400/70">Android {fd.androidVersion} · Chrome {fd.chromeVersion}</div>
                                <div className="text-purple-400/50 text-[9px]">{fd.platform}</div>
                                {/* Screen */}
                                <div className="text-cyan-400/80">{fd.screenW}×{fd.screenH} dpr={fd.dpr}</div>
                                {/* GPU */}
                                <div className="text-blue-400/70">{fd.webglVendor}</div>
                                <div className="text-blue-400/50 break-words">{fd.webglRenderer}</div>
                                {/* Hardware */}
                                <div className="text-green-400/70">CPU {fd.hwConcurrency}c · RAM {fd.deviceMemory}GB · Touch {fd.maxTouchPoints}pt</div>
                                {/* Locale */}
                                <div className="text-yellow-400/70 flex items-center gap-1 flex-wrap">
                                  <span>{fd.language} · {fd.timezone}</span>
                                  {fd.geoLocked === false && (
                                    <span
                                      title="Timezone NOT geo-locked — may not match proxy exit IP. Run again or enable Fresh Device to fix."
                                      className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-400 text-[9px] font-bold cursor-help"
                                    >
                                      <AlertTriangle className="w-2.5 h-2.5" />
                                      TZ?
                                    </span>
                                  )}
                                </div>
                                {/* Battery + Network */}
                                <div className="text-orange-400/70">
                                  {bat}{dtH ? ` (${dtH} left)` : ""} · {fd.connectionDownlink}Mbps {fd.connectionRtt}ms
                                </div>
                                {/* Browser misc */}
                                <div className="text-muted-foreground/60">History {fd.historyLength} · DNT:{dnt}</div>
                                {/* Noise seeds */}
                                <div className="text-muted-foreground/40 text-[9px]">canvas={fd.canvasSeed} · audio={fd.audioNoise} · wgl={fd.webglNoise}</div>
                              </div>
                            );
                          })()}
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
    </div>
  );
}

/* ───────────────────────── SHARED HELPERS ───────────────────────── */
function download(text: string, filename: string, mime = "text/plain") {
  const blob = new Blob([text], { type: mime });
  downloadBlob(blob, filename);
}

function downloadBlob(blob: Blob, filename: string) {
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
