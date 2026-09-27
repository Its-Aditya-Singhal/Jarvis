//! Desktop shell: starts the local Python backend on a random loopback port
//! with a per-launch API token, and hands both to the UI.
//!
//! The packaged app carries the backend as a PyInstaller folder in
//! `Contents/Resources/backend/` (see scripts/build_dmg.sh); a development
//! checkout runs `backend/.venv/bin/python -m jarvis` instead.

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{Manager, RunEvent};

#[derive(Clone, Serialize)]
struct BackendInfo {
    port: u16,
    token: String,
    /// why the backend couldn't be started (shown on the boot screen)
    error: Option<String>,
}

struct Backend {
    info: Mutex<BackendInfo>,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn backend_info(state: tauri::State<Backend>) -> BackendInfo {
    state.info.lock().unwrap().clone()
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("no free loopback port")
}

/// The packaged backend, if this is the packaged app.
fn bundled_backend(resources: Option<PathBuf>) -> Option<PathBuf> {
    let bin = resources?.join("backend").join("jarvis-backend");
    bin.is_file().then_some(bin)
}

/// Development layout: <repo>/app/src-tauri -> <repo>/backend.
fn dev_backend_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend")
}

fn backend_command(resources: Option<PathBuf>, data_dir: Option<PathBuf>) -> Command {
    match bundled_backend(resources) {
        Some(bin) => {
            let mut cmd = Command::new(bin);
            if let Some(dir) = data_dir.filter(|d| std::fs::create_dir_all(d).is_ok()) {
                cmd.current_dir(dir);
            }
            cmd
        }
        None => {
            let dir = dev_backend_dir();
            let mut cmd = Command::new(dir.join(".venv/bin/python"));
            cmd.args(["-m", "jarvis"]).current_dir(&dir);
            cmd
        }
    }
}

fn spawn_backend(mut cmd: Command, info: &BackendInfo) -> std::io::Result<Child> {
    cmd.env("JARVIS_PORT", info.port.to_string())
        .env("JARVIS_API_TOKEN", &info.token)
        .env("JARVIS_WATCH_PARENT", "1")
        .spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let info = BackendInfo {
        port: free_port(),
        token: uuid::Uuid::new_v4().simple().to_string(),
        error: None,
    };

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Backend {
            info: Mutex::new(info),
            child: Mutex::new(None),
        })
        .setup(|app| {
            let state = app.state::<Backend>();
            let cmd = backend_command(
                app.path().resource_dir().ok(),
                app.path().app_data_dir().ok(),
            );
            let mut info = state.info.lock().unwrap();
            match spawn_backend(cmd, &info) {
                Ok(c) => *state.child.lock().unwrap() = Some(c),
                Err(e) => {
                    eprintln!("failed to start backend: {e}");
                    info.error = Some(format!("The assistant's engine could not start: {e}"));
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut c) = handle.state::<Backend>().child.lock().unwrap().take() {
                // ask politely first: the backend then releases the camera and mic and
                // unloads its AI models (gigabytes Ollama would otherwise keep for a while)
                let _ = std::process::Command::new("kill")
                    .args(["-TERM", &c.id().to_string()])
                    .status();
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(4);
                while std::time::Instant::now() < deadline {
                    if let Ok(Some(_)) = c.try_wait() {
                        return;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                let _ = c.kill();
                let _ = c.wait();
            }
        }
    });
}
