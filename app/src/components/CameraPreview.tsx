import { useStore } from "../lib/store";

/** Low-res mirrored camera view with face brackets. Nothing is stored. */
export default function CameraPreview({ round = false, className = "" }: { round?: boolean; className?: string }) {
  const { preview, status } = useStore();
  const cam = status?.camera;

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
          {cam?.status === "error" ? cam.error : cam?.status === "active" ? "Starting sensor…" : "Camera starting…"}
        </div>
      )}
    </div>
  );
}
