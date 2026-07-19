import React, { useState } from "react"
import { useCheckEmails, useGetEmailStats } from "@workspace/api-client-react"
import type { EmailResult, EmailStats } from "@workspace/api-client-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { Textarea } from "@/components/ui/textarea"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Download, Terminal, CheckCircle2, XCircle, AlertTriangle, HelpCircle, Activity } from "lucide-react"

type FilterType = "all" | "valid" | "invalid" | "catch_all" | "unknown";

export default function Home() {
  const [inputText, setInputText] = useState("");
  const [results, setResults] = useState<EmailResult[]>([]);
  const [stats, setStats] = useState<EmailStats | null>(null);
  const [filter, setFilter] = useState<FilterType>("all");
  const [progress, setProgress] = useState(0);

  const checkEmailsMutation = useCheckEmails();
  const getStatsMutation = useGetEmailStats();
  
  const handleCheck = () => {
    if (!inputText.trim()) return;
    
    // Parse emails
    const rawEmails = inputText
      .split(/[\n,]+/)
      .map(e => e.trim())
      .filter(e => e.length > 0);
      
    if (rawEmails.length === 0) return;
    
    // reset state
    setResults([]);
    setStats(null);
    setFilter("all");
    setProgress(10);
    
    // simulate progress
    const progressInterval = setInterval(() => {
      setProgress(p => Math.min(p + 10, 90));
    }, 500);

    checkEmailsMutation.mutate(
      { data: { emails: rawEmails } },
      {
        onSuccess: (data) => {
          clearInterval(progressInterval);
          setProgress(100);
          setResults(data.results);
          
          // update stats
          getStatsMutation.mutate(
            { data: { results: data.results } },
            {
              onSuccess: (statsData) => {
                setStats(statsData);
              }
            }
          );
        },
        onError: () => {
          clearInterval(progressInterval);
          setProgress(0);
        }
      }
    );
  };
  
  const handleExport = () => {
    if (results.length === 0) return;
    
    const filtered = filter === 'all' 
      ? results 
      : results.filter(r => r.status === filter);
      
    const text = filtered.map(r => r.email).join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `emails_${filter}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const filteredResults = filter === "all" 
    ? results 
    : results.filter(r => r.status === filter);

  return (
    <div className="min-h-screen bg-background text-foreground p-6 font-sans">
      <div className="max-w-6xl mx-auto space-y-6">
        
        {/* Header */}
        <header className="flex items-center justify-between pb-4 border-b border-border">
          <div className="flex items-center gap-3">
            <div className="bg-primary/10 p-2 rounded-lg border border-primary/20">
              <Terminal className="w-5 h-5 text-primary" />
            </div>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">Vanguard MX</h1>
              <p className="text-sm text-muted-foreground font-mono">SMTP Protocol Verification</p>
            </div>
          </div>
          <div className="flex items-center gap-2 text-sm text-muted-foreground bg-card px-3 py-1.5 rounded-full border border-border">
            <Activity className="w-4 h-4 text-primary" />
            <span className="font-mono">System Online</span>
          </div>
        </header>

        {/* Main Content */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          
          {/* Left Column: Input */}
          <div className="lg:col-span-1 space-y-4">
            <Card className="border-border bg-card/50 backdrop-blur-sm">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm font-medium uppercase tracking-wider text-muted-foreground">Target Payload</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <Textarea
                    placeholder="Enter emails (one per line or comma-separated)"
                    className="min-h-[300px] resize-y bg-background/50 font-mono text-sm leading-relaxed"
                    value={inputText}
                    onChange={(e) => setInputText(e.target.value)}
                  />
                  <Button 
                    className="w-full font-mono font-medium tracking-wide" 
                    size="lg"
                    onClick={handleCheck}
                    disabled={checkEmailsMutation.isPending || !inputText.trim()}
                  >
                    {checkEmailsMutation.isPending ? "VERIFYING..." : "INITIATE SCAN"}
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>

          {/* Right Column: Results & Stats */}
          <div className="lg:col-span-2 space-y-6">
            
            {/* Stats Grid */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatCard title="TOTAL" value={stats?.total ?? 0} />
              <StatCard title="VALID" value={stats?.valid ?? 0} trend="valid" icon={<CheckCircle2 className="w-4 h-4" />} />
              <StatCard title="INVALID" value={stats?.invalid ?? 0} trend="invalid" icon={<XCircle className="w-4 h-4" />} />
              <StatCard title="CATCH-ALL" value={stats?.catchAll ?? 0} trend="catchall" icon={<AlertTriangle className="w-4 h-4" />} />
              <StatCard title="UNKNOWN" value={stats?.unknown ?? 0} trend="unknown" icon={<HelpCircle className="w-4 h-4" />} />
            </div>

            {checkEmailsMutation.isPending && (
              <div className="space-y-2">
                <div className="flex justify-between text-xs font-mono text-muted-foreground uppercase tracking-wider">
                  <span>Establishing SMTP Handshakes...</span>
                  <span>{progress}%</span>
                </div>
                <Progress value={progress} className="h-1 bg-border" />
              </div>
            )}

            {/* Results Table Area */}
            <Card className="border-border bg-card/50 backdrop-blur-sm min-h-[400px] flex flex-col overflow-hidden">
              <div className="border-b border-border p-3 flex flex-wrap gap-2 items-center justify-between bg-card">
                <div className="flex flex-wrap gap-1">
                  <FilterButton active={filter === 'all'} onClick={() => setFilter('all')}>ALL</FilterButton>
                  <FilterButton active={filter === 'valid'} onClick={() => setFilter('valid')} className="hover:text-valid data-[active=true]:text-valid data-[active=true]:bg-valid/10">VALID</FilterButton>
                  <FilterButton active={filter === 'invalid'} onClick={() => setFilter('invalid')} className="hover:text-invalid data-[active=true]:text-invalid data-[active=true]:bg-invalid/10">INVALID</FilterButton>
                  <FilterButton active={filter === 'catch_all'} onClick={() => setFilter('catch_all')} className="hover:text-catchall data-[active=true]:text-catchall data-[active=true]:bg-catchall/10">CATCH-ALL</FilterButton>
                  <FilterButton active={filter === 'unknown'} onClick={() => setFilter('unknown')} className="hover:text-unknown data-[active=true]:text-unknown data-[active=true]:bg-unknown/10">UNKNOWN</FilterButton>
                </div>
                <Button variant="outline" size="sm" onClick={handleExport} disabled={results.length === 0} className="font-mono text-xs h-8">
                  <Download className="w-3 h-3 mr-2" />
                  EXPORT .TXT
                </Button>
              </div>

              <div className="flex-1 overflow-auto">
                {results.length > 0 ? (
                  <Table>
                    <TableHeader className="bg-background/50 sticky top-0 backdrop-blur-sm z-10">
                      <TableRow className="hover:bg-transparent">
                        <TableHead className="font-mono text-xs tracking-wider">EMAIL</TableHead>
                        <TableHead className="font-mono text-xs tracking-wider w-[100px]">STATUS</TableHead>
                        <TableHead className="font-mono text-xs tracking-wider w-[100px]">CODE</TableHead>
                        <TableHead className="font-mono text-xs tracking-wider">DIAGNOSTIC</TableHead>
                        <TableHead className="font-mono text-xs tracking-wider w-[80px]">GMAIL</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filteredResults.map((result, idx) => (
                        <TableRow key={idx} className="font-mono text-sm group">
                          <TableCell className="font-medium text-foreground/90">{result.email}</TableCell>
                          <TableCell>
                            <StatusBadge status={result.status} />
                          </TableCell>
                          <TableCell className="text-muted-foreground">{result.smtpCode || "—"}</TableCell>
                          <TableCell className="text-muted-foreground truncate max-w-[200px]" title={result.reason}>
                            {result.reason}
                          </TableCell>
                          <TableCell>
                            {result.isGmail ? (
                              <span className="text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded border border-primary/20">YES</span>
                            ) : (
                              <span className="text-xs text-muted-foreground">—</span>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                ) : (
                  <div className="h-full flex flex-col items-center justify-center text-muted-foreground font-mono text-sm p-8 opacity-50">
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
  );
}

function StatCard({ title, value, trend, icon }: { title: string, value: number, trend?: string, icon?: React.ReactNode }) {
  const isZero = value === 0;
  return (
    <Card className="bg-card/40 border-border">
      <CardContent className="p-4 flex flex-col gap-1">
        <div className="flex items-center justify-between text-muted-foreground">
          <span className="text-[10px] font-mono tracking-widest uppercase">{title}</span>
          {icon && <span className={`opacity-60 ${!isZero && trend === 'valid' ? 'text-valid' : ''} ${!isZero && trend === 'invalid' ? 'text-invalid' : ''} ${!isZero && trend === 'catchall' ? 'text-catchall' : ''}`}>{icon}</span>}
        </div>
        <span className={`text-2xl font-mono font-medium ${
          isZero ? 'text-muted-foreground/30' : 
          trend === 'valid' ? 'text-valid' :
          trend === 'invalid' ? 'text-invalid' :
          trend === 'catchall' ? 'text-catchall' :
          'text-foreground'
        }`}>
          {value.toLocaleString()}
        </span>
      </CardContent>
    </Card>
  )
}

function FilterButton({ active, children, onClick, className }: { active?: boolean, children: React.ReactNode, onClick: () => void, className?: string }) {
  return (
    <button
      onClick={onClick}
      data-active={active}
      className={cn(
        "px-3 py-1.5 text-xs font-mono font-medium rounded-md transition-colors border",
        active 
          ? "bg-secondary text-secondary-foreground border-border" 
          : "bg-transparent text-muted-foreground border-transparent hover:bg-muted/50 hover:text-foreground",
        className
      )}
    >
      {children}
    </button>
  )
}

function StatusBadge({ status }: { status: EmailResult["status"] }) {
  const props = {
    valid: { variant: "valid", label: "VALID" },
    invalid: { variant: "invalid", label: "INVALID" },
    catch_all: { variant: "catchall", label: "CATCH-ALL" },
    unknown: { variant: "unknown", label: "UNKNOWN" }
  }[status] || { variant: "outline", label: status.toUpperCase() };

  return <Badge variant={props.variant as any} className="uppercase font-mono tracking-wider text-[10px] py-0">{props.label}</Badge>;
}
