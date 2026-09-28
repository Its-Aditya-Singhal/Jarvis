import { usePreview, useStore } from "../lib/store";

/** Low-res mirrored camera view with face brackets. Nothing is stored. */
export default function CameraPreview({ round = false, className = "" }: { round?: boolean; className?: string }) {
  const { status, auth, connected } = useStore();
  const cam = status?.camera;
  // only a running camera shows frames: after it stops (or the backend goes away) the
  // last frame would otherwise stay up, looking as if the camera were still live
  const live = connected && cam?.status === "active";
  const frame = usePreview();
  const preview = live ? frame : null;
  const name = status?.assistant_name ?? "JARVIS";

  if (auth?.face_once) {
    return (
      <div className={`camera ${round ? "round" : ""} ${className}`}>
        <div className="camera-empty camera-off">
          <span className="camera-off-title">FACE VERIFIED · CAMERA OFF</span>
          <span>Your voice keeps you signed in until {name} restarts.</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`camera ${round ? "round" : ""} ${className}`}>
      {preview ? (
        <>
          <img src={preview.src} alt="Camera preview" />
          {preview.boxes.map((b, i) => (
            <div
              key={i}
              className="face-box"
              style={{
                // preview is mirrored, so mirror the box too
                left: `${(1 - b[2]) * 100}%`,
                top: `${b[1] * 100}%`,
                width: `${(b[2] - b[0]) * 100}%`,
                height: `${(b[3] - b[1]) * 100}%`,
              }}
            />
          ))}
          <div className="scanline" />
        </>
      ) : (
        <div className="camera-empty">
          {!connected
            ? "Camera off"
            : cam?.status === "error"
              ? cam.error
              : cam?.status === "active"
                ? "Starting sensor…"
                : cam?.status === "off"
                  ? "Camera off"
                  : "Camera starting…"}
        </div>
      )}
    </div>
  );
}
