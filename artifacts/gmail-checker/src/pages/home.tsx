import React, { useState } from "react"
import { useCheckEmails, useGetEmailStats, useLoginCheckEmails } from "@workspace/api-client-react"
import type { EmailResult, EmailStats, LoginResult } from "@workspace/api-client-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import {
  Download, Terminal, CheckCircle2, XCircle, AlertTriangle,
  HelpCircle, Activity, ShieldAlert, KeyRound, Smartphone,
  Lock, MailCheck, MailX, RefreshCw
} from "lucide-react"

type SmtpFilter = "all" | "valid" | "invalid" | "disabled" | "catch_all" | "unknown";
type Mode = "smtp" | "login";
type LoginList = "opened" | "not_opened";

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
        <div className="flex gap-2">
          <button
            onClick={() => setMode("smtp")}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-mono font-medium transition-colors",
              mode === "smtp"
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-card border-border text-muted-foreground hover:text-foreground hover:bg-muted/50"
            )}
          >
            <Terminal className="w-4 h-4" />
            SMTP CHECK
          </button>
          <button
            onClick={() => setMode("login")}
            className={cn(
              "flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-mono font-medium transition-colors",
              mode === "login"
                ? "bg-primary text-primary-foreground border-primary"
                : "bg-card border-border text-muted-foreground hover:text-foreground hover:bg-muted/50"
            )}
          >
            <KeyRound className="w-4 h-4" />
            LOGIN CHECK
          </button>
        </div>

        {mode === "smtp" ? <SmtpChecker /> : <LoginChecker />}
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
        onSuccess: (data) => {
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
        const password = parts.slice(1, parts.length > 3 ? -1 : undefined).join(":").trim();
        const totp = parts.length > 3 ? parts[parts.length - 1].trim() : (
          // handle email:password:totp (exactly 3 segments after splitting on first colon)
          parts.length === 3 ? parts[2].trim() : undefined
        );
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
        onSuccess: (data) => { clearInterval(iv); setProgress(100); setResults(data.results); },
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

/* ───────────────────────── SHARED HELPERS ───────────────────────── */
function download(text: string, filename: string) {
  const blob = new Blob([text], { type: "text/plain" });
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
