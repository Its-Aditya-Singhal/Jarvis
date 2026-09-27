// Opens a web page in the default browser (the app's own window never navigates away).
export async function openUrl(url: string): Promise<void> {
  if ("__TAURI_INTERNALS__" in window) {
    const { openUrl: open } = await import("@tauri-apps/plugin-opener");
    await open(url);
  } else {
    window.open(url, "_blank", "noopener");
  }
}
