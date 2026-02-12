import React, { useEffect, useRef } from "react";

export default function CameraSheet({ stream, status }) {
  const videoRef = useRef(null);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (stream) v.srcObject = stream;
    return () => {
      try { v.srcObject = null; } catch {}
    };
  }, [stream]);

  return (
    <div className="space-y-3 text-white/80">
      <div className="text-xs text-white/60">Status: {status}</div>
      <div className="rounded-2xl border border-white/10 bg-black/35 overflow-hidden">
        <video ref={videoRef} autoPlay playsInline muted className="w-full h-[240px] object-cover" />
      </div>
      <div className="text-xs text-white/60">
        This is the camera feed used by vision + hand tracking.
      </div>
    </div>
  );
}
