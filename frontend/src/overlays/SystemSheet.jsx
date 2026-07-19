import React, { useEffect, useMemo, useRef, useState } from "react";
import { Cpu, Thermometer, MemoryStick, Gauge, Eye, Mic2, Volume2, PlugZap, RefreshCw } from "lucide-react";

function apiUrl(path) {
  try {
    const base = window.__NOVA_API_BASE || "http://localhost:8008";
    return `${base}${path}`;
  } catch {
    return `http://localhost:8008${path}`;
  }
}

function Row({ icon: Icon, label, value, ok }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-nova-gold/10 bg-black/25 px-3 py-2">
      <div className="inline-flex items-center gap-2 text-xs text-nova-gold/60 uppercase tracking-[0.14em]">
        <Icon size={13} />
        {label}
      </div>
      <div
        className={[
          "text-sm text-right break-words",
          ok === false ? "text-red-300" : ok === true ? "text-emerald-300" : "text-nova-gold/90",
        ].join(" ")}
      >
        {value}
      </div>
    </div>
  );
}

export default function SystemSheet({ liveEvents = [], eventsConnected = false }) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const logRef = useRef(null);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/status"));
      if (!res.ok) throw new Error(await res.text());
      setStatus(await res.json());
    } catch (err) {
      setError(String(err?.message || err));
      setStatus(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const recentLog = useMemo(() => liveEvents.slice(-40).reverse(), [liveEvents]);

  const gpu = status?.gpu;
  const model = status?.model;
  const integrations = status?.integrations || {};

  return (
    <div className="space-y-3 text-nova-gold h-full flex flex-col">
      <div className="flex items-center justify-between">
        <div className="inline-flex items-center gap-2 text-sm text-nova-gold/80">
          <Cpu size={16} />
          System status
          <span
            className={[
              "ml-2 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.14em]",
              eventsConnected
                ? "border-emerald-400/25 bg-emerald-500/10 text-emerald-300"
                : "border-red-400/25 bg-red-500/10 text-red-300",
            ].join(" ")}
          >
            <PlugZap size={10} />
            {eventsConnected ? "Event stream live" : "Event stream offline"}
          </span>
        </div>
        <button
          type="button"
          onClick={load}
          className="rounded-xl px-3 py-2 bg-black/25 border border-nova-gold/20 text-nova-gold/70 hover:text-nova-gold hover:bg-black/35 text-sm"
          title="Refresh"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
        </button>
      </div>

      {error && (
        <div className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-xs text-red-200">
          Backend unreachable: {error}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 flex-1 min-h-0">
        <div className="min-h-0 overflow-y-auto space-y-2 pr-1">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50">Core</div>
          <Row icon={Cpu} label="Model" value={model?.name || "No model found"} ok={model?.loaded ? true : undefined} />
          <Row
            icon={Gauge}
            label="GPU offload"
            value={String(model?.enforcement?.status || "unknown").replace(/_/g, " ")}
            ok={Boolean(model?.enforcement?.active)}
          />
          <Row icon={MemoryStick} label="Context window" value={`${(model?.context_tokens || 0).toLocaleString()} tokens`} />

          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pt-2">GPU</div>
          {gpu?.available ? (
            <>
              <Row icon={Cpu} label="GPU" value={gpu.name || "Unknown"} />
              <Row
                icon={MemoryStick}
                label="VRAM"
                value={`${(gpu.vram_used_mb ?? 0).toLocaleString()} / ${(gpu.vram_total_mb ?? 0).toLocaleString()} MB`}
              />
              <Row icon={Thermometer} label="Temperature" value={`${gpu.temperature_c ?? "--"}°C`} />
              <Row icon={Gauge} label="Utilization" value={`${gpu.utilization_pct ?? "--"}%`} />
            </>
          ) : (
            <Row icon={Cpu} label="GPU telemetry" value={gpu?.error || "Unavailable"} ok={false} />
          )}

          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pt-2">Subsystems</div>
          <Row icon={Eye} label="Vision" value={status?.vision?.enabled ? "Ready" : status?.vision?.reason || "Not configured"} ok={status?.vision?.enabled ? true : undefined} />
          <Row icon={Volume2} label="TTS (XTTS)" value={status?.tts?.loaded ? `Loaded (${status.tts.device})` : "Loads on first use"} />
          <Row icon={Mic2} label="STT" value={status?.stt?.loaded ? "Loaded" : `Loads on first use (${status?.stt?.model || "whisper"})`} />

          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pt-2">Integrations</div>
          {Object.entries(integrations).map(([name, info]) => (
            <Row
              key={name}
              icon={PlugZap}
              label={name.replace(/_/g, " ")}
              value={info?.configured ? "Configured" : `Not configured (${(info?.requires || []).join(", ") || "no key needed"})`}
              ok={info?.configured ? true : name === "web_search" ? true : false}
            />
          ))}
        </div>

        <div className="min-h-0 flex flex-col rounded-2xl border border-nova-gold/10 bg-black/20 p-3">
          <div className="text-[10px] uppercase tracking-[0.24em] text-nova-gold/50 pb-2">Live event log</div>
          <div ref={logRef} className="flex-1 min-h-0 overflow-y-auto space-y-1 font-mono text-[11px]">
            {recentLog.length ? (
              recentLog.map((ev) => (
                <div key={ev.seq} className="rounded border border-nova-gold/5 bg-black/25 px-2 py-1">
                  <span className="text-nova-purple/90">{String(ev.ts || "").slice(11, 19)}</span>{" "}
                  <span className="text-nova-gold/85">{ev.type}</span>
                  {ev?.data && Object.keys(ev.data).length ? (
                    <span className="text-nova-gold/45"> {JSON.stringify(ev.data).slice(0, 140)}</span>
                  ) : null}
                </div>
              ))
            ) : (
              <div className="text-nova-gold/40 pt-4 text-center">
                {eventsConnected ? "Waiting for events…" : "Connect to the backend to see live events."}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
