import { useCallback, useEffect, useState } from "react";
import { Loader2, Wallet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch, parseApiJson } from "@/lib/apiBase";

type GmailPayStatus = {
  ok?: boolean;
  chatTarget?: string;
  liveSaveEnabled?: boolean;
  error?: string;
};

export function GmailPaySave() {
  const [chatTarget, setChatTarget] = useState("Gmailpaybot");
  const [liveSaveEnabled, setLiveSaveEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await apiFetch("/api/gmail-pay/status", { timeoutMs: 10_000 });
      const data = await parseApiJson<GmailPayStatus>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      if (data.chatTarget) setChatTarget(data.chatTarget);
      setLiveSaveEnabled(Boolean(data.liveSaveEnabled));
      setMessage("Saved Gmail Pay settings load ho gayi.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Status load nahi hui");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const res = await apiFetch("/api/gmail-pay/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chatTarget, liveSaveEnabled }),
        timeoutMs: 10_000,
      });
      const data = await parseApiJson<GmailPayStatus>(res);
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
      if (data.chatTarget) setChatTarget(data.chatTarget);
      setLiveSaveEnabled(Boolean(data.liveSaveEnabled));
      setMessage("Gmail Pay save ho gaya.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save fail");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="border-border bg-card/50">
      <CardHeader className="pb-3">
        <CardTitle className="text-xs font-mono uppercase tracking-wider text-muted-foreground flex items-center gap-2">
          <Wallet className="w-4 h-4 text-primary" />
          Gmail Pay Save
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 max-w-xl">
        <p className="text-xs font-mono text-muted-foreground leading-relaxed">
          Bot target aur live save. Pehle se saved setting yahin rehti hai.
        </p>
        <label className="block space-y-1">
          <span className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">Bot</span>
          <Input
            value={chatTarget}
            onChange={(event) => setChatTarget(event.target.value)}
            className="font-mono"
            placeholder="Gmailpaybot"
            disabled={loading || saving}
          />
        </label>
        <button
          type="button"
          onClick={() => setLiveSaveEnabled((value) => !value)}
          className="flex items-center justify-between w-full rounded-lg border border-border px-3 py-2 text-sm font-mono"
          disabled={loading || saving}
        >
          <span>Live save</span>
          <span className={liveSaveEnabled ? "text-primary" : "text-muted-foreground"}>
            {liveSaveEnabled ? "ON" : "OFF"}
          </span>
        </button>
        <div className="flex items-center gap-2">
          <Button type="button" onClick={() => void save()} disabled={loading || saving} className="font-mono">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : "SAVE"}
          </Button>
          <Button type="button" variant="outline" onClick={() => void load()} disabled={loading || saving} className="font-mono">
            RELOAD
          </Button>
        </div>
        {message ? <p className="text-xs font-mono text-primary">{message}</p> : null}
        {error ? <p className="text-xs font-mono text-red-400">{error}</p> : null}
      </CardContent>
    </Card>
  );
}
