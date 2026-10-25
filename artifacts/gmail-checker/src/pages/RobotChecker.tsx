import React, { useEffect, useRef, useState } from "react";
import type { BrowserLoginResult } from "@workspace/api-client-react";
import { getRobotCheckBucket, type RobotCheckBucket } from "@/lib/robotResultCategory";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  Bot, CheckCircle2, Globe, HelpCircle, Loader2, Lock, PauseCircle, PlayCircle,
  RefreshCw, ShieldAlert, Terminal, XCircle,
} from "lucide-react";

type ExtResult = BrowserLoginResult & {
  status: BrowserLoginResult["status"] | "checking";
  robotOutcome?: string;
};

type ResultTab = RobotCheckBucket | "checking";

const LS = {
  input: "vrc_input",
  proxy: "vrc_proxy",
  proxyMode: "vrc_proxy_mode",
  fresh: "vrc_fresh",
  device: "vrc_device",
  results: "vrc_results",
  total: "vrc_total",
  jobId: "vrc_job_id",
  savedAt: "vrc_saved_at",
} as const;

function lsGet<T>(key: string, fb: T): T {
  try {
    const v = localStorage.getItem(key);
    return v != null ? JSON.parse(v) : fb;
  } catch {
    return fb;
  }
}

function parseEmails(text: string): string[] {
  return text
    .split(/\n+/)
    .map(l => l.trim())
    .filter(Boolean)
    .map(line => line.split(":")[0]?.trim() ?? "")
    .filter(e => e.includes("@"));
}

function bucketLabel(bucket: RobotCheckBucket): string {
  if (bucket === "not_robot") return "Not robot";
  if (bucket === "robot") return "Robot";
  return "Unknown";
}

function download(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function RobotChecker() {
  const [inputText, setInputText] = useState(() => lsGet(LS.input, ""));
  const [proxyText, setProxyText] = useState(() => lsGet(LS.proxy, ""));
  const [proxyMode, setProxyMode] = useState<"saved" | "custom">(
    () => (lsGet<string>(LS.proxyMode, "saved") === "custom" ? "custom" : "saved"),
  );
  const [savedProxy, setSavedProxy] = useState<{ configured: boolean; label: string } | null>(null);
  const [freshProfile, setFreshProfile] = useState(() => lsGet(LS.fresh, false));
  const deviceIndex = lsGet(LS.device, 0);
  const [activeTab, setActiveTab] = useState<ResultTab>("not_robot");
  const [results, setResults] = useState<ExtResult[]>(() => lsGet(LS.results, []));
  const [total, setTotal] = useState(() => lsGet(LS.total, 0));
  const [jobId, setJobId] = useState<string | null>(() => localStorage.getItem(LS.jobId));
  const [isRunning, setIsRunning] = useState(false);
  const [jobStatus, setJobStatus] = useState("idle");
  const [resumeReady, setResumeReady] = useState(false);
  const [jobStartError, setJobStartError] = useState("");
  const [proxyCheckState, setProxyCheckState] = useState<"idle" | "checking" | "ok" | "fail">("idle");
  const [proxyCheckError, setProxyCheckError] = useState("");
  const [browserLogs, setBrowserLogs] = useState<Array<{ email?: string; message: string }>>([]);

  const sseAbortRef = useRef<AbortController | null>(null);
  const activeJobIdRef = useRef<string | null>(jobId);

  useEffect(() => {
    localStorage.setItem(LS.input, JSON.stringify(inputText));
    localStorage.setItem(LS.proxy, JSON.stringify(proxyText));
    localStorage.setItem(LS.proxyMode, JSON.stringify(proxyMode));
    localStorage.setItem(LS.fresh, JSON.stringify(freshProfile));
    localStorage.setItem(LS.device, JSON.stringify(deviceIndex));
    localStorage.setItem(LS.results, JSON.stringify(results));
    localStorage.setItem(LS.total, JSON.stringify(total));
    if (jobId) localStorage.setItem(LS.jobId, jobId);
  }, [inputText, proxyText, proxyMode, freshProfile, deviceIndex, results, total, jobId]);

  useEffect(() => {
    fetch("/api/proxy/status")
      .then(r => r.json())
      .then(d => setSavedProxy({ configured: !!d.configured, label: d.label ?? "saved-proxy" }))
      .catch(() => setSavedProxy({ configured: false, label: "" }));
  }, []);

  useEffect(() => {
    if (jobId) restoreJobFromServer(jobId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parseProxies = (text: string) => text.split(/\n+/).map(l => l.trim()).filter(Boolean);

  const applyJobState = (job: any) => {
    setBrowserLogs(
      (job.logs ?? []).map((entry: any) => ({
        email: typeof entry.email === "string" ? entry.email : undefined,
        message: String(entry.message ?? ""),
      })).filter((e: { message: string }) => e.message),
    );
    setResults(prev => {
      const merged = [...prev];
      for (const r of job.results ?? []) {
        const row: ExtResult = { ...r };
        const idx = merged.findIndex(x => x.email === r.email);
        if (idx !== -1) merged[idx] = row;
        else merged.push(row);
      }
      for (const email of job.checkingEmails ?? []) {
        if (!merged.some(x => x.email === email)) {
          merged.push({ email, status: "checking", reason: "Checking…", totpCode: null });
        }
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
      const res = await fetch(`/api/jobs/${id}`);
      if (!res.ok) {
        setJobId(null);
        return;
      }
      const { job } = await res.json();
      if (!job) {
        setJobId(null);
        return;
      }
      applyJobState(job);
      if (job.status === "running") connectToJobStream(id, job.eventsCount ?? 0);
    } catch {
      /* ignore */
    }
  };

  const handleJobEvent = (evt: any) => {
    if (evt.type === "checking") {
      setResults(prev => {
        if (prev.some(r => r.email === evt.email)) return prev;
        return [...prev, { email: evt.email, status: "checking", reason: "Checking…", totpCode: null }];
      });
    } else if (evt.type === "result") {
      setResults(prev => {
        const next = prev.filter(r => r.email !== evt.email);
        next.push({ ...evt, status: evt.status ?? "unknown" });
        return next;
      });
    } else if (evt.type === "log") {
      setBrowserLogs(prev => [...prev.slice(-400), { email: evt.email, message: evt.message }]);
    } else if (evt.type === "done" || evt.type === "paused_done" || evt.type === "cancelled" || evt.type === "interrupted") {
      setJobStatus(evt.type === "done" ? "completed" : evt.type);
      setIsRunning(false);
    }
  };

  const connectToJobStream = (id: string, since = 0) => {
    sseAbortRef.current?.abort();
    activeJobIdRef.current = id;
    const abort = new AbortController();
    sseAbortRef.current = abort;
    (async () => {
      try {
        const resp = await fetch(`/api/jobs/${id}/stream?since=${since}`, { signal: abort.signal });
        if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const blocks = buf.split("\n\n");
          buf = blocks.pop() ?? "";
          for (const block of blocks) {
            const dataLine = block.split("\n").find(l => l.startsWith("data: "));
            if (!dataLine) continue;
            try {
              handleJobEvent(JSON.parse(dataLine.slice(6)));
            } catch {
              /* ignore */
            }
          }
        }
        const stateRes = await fetch(`/api/jobs/${id}`);
        if (stateRes.ok) {
          const { job } = await stateRes.json();
          if (job) applyJobState(job);
        }
        setIsRunning(false);
      } catch (err: any) {
        if (err?.name === "AbortError") return;
        setIsRunning(false);
      }
    })();
  };

  const ensureProxy = async (): Promise<boolean> => {
    setProxyCheckState("checking");
    setProxyCheckError("");
    try {
      if (proxyMode === "saved") {
        const res = await fetch("/api/proxy/check", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ useSaved: true }),
        });
        const d = await res.json();
        if (!d.ok) {
          setProxyCheckState("fail");
          setProxyCheckError(d.error ?? "Saved proxy failed");
          return false;
        }
        setProxyCheckState("ok");
        return true;
      }
      const proxies = parseProxies(proxyText);
      if (!proxies.length) {
        setProxyCheckState("fail");
        setProxyCheckError("Custom proxy empty");
        return false;
      }
      const d = await fetch("/api/proxy/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy: proxies[0] }),
      }).then(r => r.json());
      if (!d.ok) {
        setProxyCheckState("fail");
        setProxyCheckError(d.error ?? "Proxy failed");
        return false;
      }
      setProxyCheckState("ok");
      return true;
    } catch {
      setProxyCheckState("fail");
      setProxyCheckError("Proxy check failed");
      return false;
    }
  };

  const buildBody = (emails: string[]) => {
    const proxies = proxyMode === "custom" ? parseProxies(proxyText) : [];
    return JSON.stringify({
      robotCheckOnly: true,
      robotCheckBatch: true,
      emails,
      concurrency: 1,
      freshProfile: false,
      deviceIndex,
      ...(proxies.length > 1 ? { proxies } : proxies.length === 1 ? { proxy: proxies[0] } : {}),
    });
  };

  const handleCheck = async () => {
    const emails = parseEmails(inputText);
    if (!emails.length) return;
    if (!(await ensureProxy())) return;
    sseAbortRef.current?.abort();
    setResults([]);
    setTotal(emails.length);
    setJobStartError("");
    setJobStatus("running");
    setIsRunning(true);
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: buildBody(emails),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${res.status}`);
      }
      const { jobId: id } = await res.json();
      setJobId(id);
      activeJobIdRef.current = id;
      connectToJobStream(id, 0);
    } catch (e) {
      setJobStartError(e instanceof Error ? e.message : "Could not start");
      setIsRunning(false);
      setJobStatus("idle");
    }
  };

  const handlePause = async () => {
    const id = activeJobIdRef.current ?? jobId;
    if (!id) return;
    await fetch(`/api/jobs/${id}/pause`, { method: "POST" });
  };

  const handleResume = async () => {
    const id = activeJobIdRef.current ?? jobId;
    if (!id || !resumeReady) return;
    await fetch(`/api/jobs/${id}/resume`, { method: "POST" });
    setIsRunning(true);
    const stateRes = await fetch(`/api/jobs/${id}`);
    const { job } = await stateRes.json();
    connectToJobStream(id, job?.eventsCount ?? 0);
  };

  const inFlight = results.filter(r => r.status === "checking");
  const notRobot = results.filter(r => getRobotCheckBucket(r) === "not_robot" && r.status !== "checking");
  const robotList = results.filter(r => getRobotCheckBucket(r) === "robot");
  const unknownList = results.filter(r => getRobotCheckBucket(r) === "unknown" && r.status !== "checking");
  const completed = results.filter(r => r.status !== "checking").length;
  const progress = total > 0 ? Math.round((completed / total) * 100) : 0;

  const displayed =
    activeTab === "checking" ? inFlight
    : activeTab === "not_robot" ? notRobot
    : activeTab === "robot" ? robotList
    : unknownList;

  const exportCsv = (rows: ExtResult[], name: string) => {
    const csv = (s: string) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    const body = rows.map(r =>
      [csv(r.email), csv(bucketLabel(getRobotCheckBucket(r))), csv(r.reason ?? "")].join(","),
    ).join("\n");
    download(`Email,Bucket,Reason\n${body}`, `${name}.csv`, "text/csv");
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-1 space-y-3">
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
              <Bot className="w-3.5 h-3.5 text-primary" /> Robot Check
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-[11px] text-muted-foreground leading-relaxed">
              Sirf Gmail daalo (ek line par ek). Har account <strong>ek-ek karke</strong> check hoga:
              email submit → captcha aaya to <span className="text-red-400">Robot</span>,
              seedha password page to <span className="text-green-400">Not robot</span>.
            </p>
            <Textarea
              placeholder={"user1@gmail.com\nuser2@gmail.com\nuser3@gmail.com"}
              className="min-h-[180px] font-mono text-sm bg-background/50"
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              disabled={isRunning}
            />
            {jobStartError && (
              <p className="text-[11px] text-red-400 font-mono">{jobStartError}</p>
            )}
            <div className="flex items-center gap-2 text-[11px] font-mono">
              <button
                type="button"
                onClick={() => setProxyMode("saved")}
                className={cn("px-2 py-0.5 rounded border", proxyMode === "saved" ? "border-yellow-500/40 text-yellow-300" : "text-muted-foreground")}
              >Saved proxy</button>
              <button
                type="button"
                onClick={() => setProxyMode("custom")}
                className={cn("px-2 py-0.5 rounded border", proxyMode === "custom" ? "border-yellow-500/40 text-yellow-300" : "text-muted-foreground")}
              >Custom</button>
            </div>
            {proxyMode === "custom" && (
              <Textarea
                placeholder="http://user:pass@host:port"
                className="min-h-[60px] font-mono text-xs"
                value={proxyText}
                onChange={e => setProxyText(e.target.value)}
                disabled={isRunning}
              />
            )}
            {proxyMode === "saved" && savedProxy && (
              <p className="text-[10px] font-mono text-muted-foreground">
                {savedProxy.configured ? savedProxy.label : "Server par PROXY_URL set karo"}
              </p>
            )}
            {proxyCheckState === "fail" && (
              <p className="text-[10px] text-red-400 font-mono">{proxyCheckError}</p>
            )}
            <p className="text-[10px] font-mono text-muted-foreground">
              Ek hi Chrome — har Gmail ke baad sign-in page par wapas, naya browser nahi.
            </p>
            <div className="flex gap-2 flex-wrap">
              {!isRunning ? (
                <Button onClick={handleCheck} className="flex-1" disabled={!parseEmails(inputText).length}>
                  Check {parseEmails(inputText).length || ""} account{parseEmails(inputText).length === 1 ? "" : "s"} (1-by-1)
                </Button>
              ) : (
                <Button variant="secondary" onClick={handlePause} className="flex-1">
                  <PauseCircle className="w-4 h-4 mr-1" /> Pause
                </Button>
              )}
              {resumeReady && (
                <Button variant="outline" onClick={handleResume}>
                  <PlayCircle className="w-4 h-4 mr-1" /> Resume
                </Button>
              )}
            </div>
            {total > 0 && (
              <div className="space-y-1">
                <Progress value={progress} className="h-2" />
                <p className="text-[10px] font-mono text-muted-foreground text-center">
                  {completed}/{total} · concurrency 1 (serial)
                </p>
              </div>
            )}
          </CardContent>
        </Card>
        <Card className="border-border bg-card/50">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-mono uppercase text-muted-foreground flex items-center gap-2">
              <Terminal className="w-3.5 h-3.5" /> Live log
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-48 overflow-y-auto font-mono text-[10px] text-muted-foreground space-y-0.5">
              {browserLogs.slice(-80).map((l, i) => (
                <div key={i}>
                  {l.email ? <span className="text-primary">{l.email}: </span> : null}
                  {l.message}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="lg:col-span-2 space-y-3">
        <div className="flex flex-wrap gap-2">
          {([
            ["not_robot", notRobot.length, CheckCircle2, "text-green-400"],
            ["robot", robotList.length, ShieldAlert, "text-red-400"],
            ["unknown", unknownList.length, HelpCircle, "text-yellow-400"],
            ["checking", inFlight.length, Loader2, "text-blue-400"],
          ] as const).map(([key, count, Icon, cls]) => (
            <button
              key={key}
              type="button"
              onClick={() => setActiveTab(key)}
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-lg border text-sm font-mono transition-colors",
                activeTab === key ? "border-primary bg-primary/10" : "border-border bg-card/30",
              )}
            >
              <Icon className={cn("w-4 h-4", cls)} />
              {key === "checking" ? "Running" : bucketLabel(key as RobotCheckBucket)}
              <Badge variant="secondary">{count}</Badge>
            </button>
          ))}
          <Button variant="ghost" size="sm" onClick={() => exportCsv(displayed, `robot-${activeTab}`)} disabled={!displayed.length}>
            Export CSV
          </Button>
        </div>

        <Card className="border-border">
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead>Bucket</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {displayed.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={3} className="text-center text-muted-foreground py-8 font-mono text-sm">
                      {parseEmails(inputText).length ? "Start check — accounts queue 1-by-1" : "Gmail lines paste karo"}
                    </TableCell>
                  </TableRow>
                ) : displayed.map(r => (
                  <TableRow key={r.email}>
                    <TableCell className="font-mono text-xs">{r.email}</TableCell>
                    <TableCell>
                      {r.status === "checking" ? (
                        <Loader2 className="w-4 h-4 animate-spin text-blue-400" />
                      ) : (
                        <Badge variant={getRobotCheckBucket(r) === "robot" ? "destructive" : "secondary"}>
                          {bucketLabel(getRobotCheckBucket(r))}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground max-w-md truncate">{r.reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
