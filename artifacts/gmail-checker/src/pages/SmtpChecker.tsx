import { useEffect, useState, type ReactNode } from "react"
import { useCheckEmails, useGetEmailStats } from "@workspace/api-client-react"
import type { EmailResult, EmailStats } from "@workspace/api-client-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Download, Terminal, CheckCircle2, XCircle, AlertTriangle, HelpCircle, ShieldAlert } from "lucide-react"

type SmtpFilter = "all" | "valid" | "invalid" | "disabled" | "catch_all" | "unknown"

function download(text: string, filename: string) {
  const blob = new Blob([text], { type: "text/plain" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function StatCard({ title, value, trend, icon }: { title: string; value: number; trend?: string; icon?: ReactNode }) {
  const isZero = value === 0
  return (
    <Card className="bg-card/40 border-border">
      <CardContent className="p-4 flex flex-col gap-1">
        <div className="flex items-center justify-between text-muted-foreground">
          <span className="text-[10px] font-mono tracking-widest uppercase">{title}</span>
          {icon && <span className={cn("opacity-60",
            !isZero && trend === "valid" ? "text-valid" : "",
            !isZero && trend === "invalid" ? "text-invalid" : "",
            !isZero && trend === "catchall" ? "text-catchall" : "",
          )}>{icon}</span>}
        </div>
        <span className={cn("text-2xl font-mono font-medium",
          isZero ? "text-muted-foreground/30" :
          trend === "valid" ? "text-valid" :
          trend === "invalid" ? "text-invalid" :
          trend === "disabled" ? "text-orange-400" :
          trend === "catchall" ? "text-catchall" : "text-foreground",
        )}>{value.toLocaleString()}</span>
      </CardContent>
    </Card>
  )
}

function FilterButton({ active, children, onClick }: { active?: boolean; children: ReactNode; onClick: () => void }) {
  return (
    <button onClick={onClick} type="button"
      className={cn(
        "px-3 py-1.5 text-xs font-mono font-medium rounded-md transition-colors border",
        active ? "bg-secondary text-secondary-foreground border-border" : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50 hover:text-foreground",
      )}>
      {children}
    </button>
  )
}

function StatusBadge({ status }: { status: EmailResult["status"] }) {
  if (status === "disabled") {
    return <Badge variant="outline" className="uppercase font-mono tracking-wider text-[10px] py-0 border-orange-500/50 text-orange-400 bg-orange-400/10">DISABLED</Badge>
  }
  const props = {
    valid: { variant: "valid" as const, label: "VALID" },
    invalid: { variant: "invalid" as const, label: "INVALID" },
    catch_all: { variant: "catchall" as const, label: "CATCH-ALL" },
    unknown: { variant: "unknown" as const, label: "UNKNOWN" },
  }[status] ?? { variant: "outline" as const, label: status.toUpperCase() }
  return <Badge variant={props.variant} className="uppercase font-mono tracking-wider text-[10px] py-0">{props.label}</Badge>
}

export default function SmtpChecker() {
  const [inputText, setInputText] = useState("")
  const [results, setResults] = useState<EmailResult[]>([])
  const [stats, setStats] = useState<EmailStats | null>(null)
  const [filter, setFilter] = useState<SmtpFilter>("all")
  const [progress, setProgress] = useState(0)
  const checkEmailsMutation = useCheckEmails()
  const getStatsMutation = useGetEmailStats()

  useEffect(() => {
    document.title = "SMTP Check"
  }, [])

  const handleCheck = () => {
    const rawEmails = inputText.split(/[\n,]+/).map(e => e.trim()).filter(Boolean)
    if (rawEmails.length === 0) return
    setResults([])
    setStats(null)
    setFilter("all")
    setProgress(10)
    const iv = setInterval(() => setProgress(p => Math.min(p + 10, 90)), 500)
    checkEmailsMutation.mutate(
      { data: { emails: rawEmails } },
      {
        onSuccess: (data) => {
          clearInterval(iv)
          setProgress(100)
          setResults(data.results)
          getStatsMutation.mutate({ data: { results: data.results } }, { onSuccess: setStats })
        },
        onError: () => {
          clearInterval(iv)
          setProgress(0)
        },
      },
    )
  }

  const filtered = filter === "all" ? results : results.filter(r => r.status === filter)

  const handleExport = () => {
    download(
      filtered.map(r => [r.email, r.status, r.smtpCode || "", r.reason || ""].join(":")).join("\n"),
      `emails_${filter}.txt`,
    )
  }

  return (
    <div className="min-h-screen bg-background text-foreground p-4 md:p-6 font-sans">
      <div className="max-w-6xl mx-auto space-y-5">
        <header className="flex items-center gap-3 pb-4 border-b border-border">
          <div className="bg-primary/10 p-2 rounded-lg border border-primary/20">
            <Terminal className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">SMTP Check</h1>
            <p className="text-sm text-muted-foreground font-mono">Mailbox verification</p>
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
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

          <div className="lg:col-span-2 space-y-5">
            <div className="grid grid-cols-3 md:grid-cols-6 gap-3">
              <StatCard title="TOTAL" value={stats?.total ?? 0} />
              <StatCard title="VALID" value={stats?.valid ?? 0} trend="valid" icon={<CheckCircle2 className="w-4 h-4" />} />
              <StatCard title="INVALID" value={stats?.invalid ?? 0} trend="invalid" icon={<XCircle className="w-4 h-4" />} />
              <StatCard title="DISABLED" value={stats?.disabled ?? 0} trend="disabled" icon={<ShieldAlert className="w-4 h-4" />} />
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
                  {(["all", "valid", "invalid", "disabled", "catch_all", "unknown"] as SmtpFilter[]).map(f => (
                    <FilterButton key={f} active={filter === f} onClick={() => setFilter(f)}>{f.replace("_", " ").toUpperCase()}</FilterButton>
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
                        <TableHead className="font-mono text-xs w-[48px] text-center">#</TableHead>
                        <TableHead className="font-mono text-xs">EMAIL</TableHead>
                        <TableHead className="font-mono text-xs w-[100px]">STATUS</TableHead>
                        <TableHead className="font-mono text-xs w-[70px]">CODE</TableHead>
                        <TableHead className="font-mono text-xs min-w-[160px]">DIAGNOSTIC</TableHead>
                        <TableHead className="font-mono text-xs w-[70px]">GMAIL</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filtered.map((r, idx) => (
                        <TableRow key={`${r.email}-${idx}`} className="font-mono text-sm">
                          <TableCell className="text-center text-muted-foreground/50 text-xs tabular-nums">{idx + 1}</TableCell>
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
                  <div className="h-full min-h-[200px] flex flex-col items-center justify-center text-muted-foreground font-mono text-sm p-8 opacity-50">
                    <Terminal className="w-8 h-8 mb-3 opacity-50" />
                    <p>AWAITING PAYLOAD</p>
                  </div>
                )}
              </div>
            </Card>
          </div>
        </div>
      </div>
    </div>
  )
}
