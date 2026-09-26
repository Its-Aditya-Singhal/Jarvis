//! Desktop shell: starts the local Python backend on a random loopback port
//! with a per-launch API token, and hands both to the UI.

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
}

struct Backend {
    info: BackendInfo,
    child: Mutex<Option<Child>>,
}

#[tauri::command]
fn backend_info(state: tauri::State<Backend>) -> BackendInfo {
    state.info.clone()
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("no free loopback port")
}

/// Development layout: <repo>/app/src-tauri -> <repo>/backend.
/// The packaged sidecar arrives in the packaging phase.
fn backend_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend")
}

fn spawn_backend(info: &BackendInfo) -> std::io::Result<Child> {
    let dir = backend_dir();
    Command::new(dir.join(".venv/bin/python"))
        .args(["-m", "jarvis"])
        .current_dir(&dir)
        .env("JARVIS_PORT", info.port.to_string())
        .env("JARVIS_API_TOKEN", &info.token)
        .env("JARVIS_WATCH_PARENT", "1")
        .spawn()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let info = BackendInfo {
        port: free_port(),
        token: uuid::Uuid::new_v4().simple().to_string(),
    };
    let child = match spawn_backend(&info) {
        Ok(c) => Some(c),
        Err(e) => {
            eprintln!("failed to start backend: {e}");
            None
        }
    };

    let app = tauri::Builder::default()
        .manage(Backend {
            info,
            child: Mutex::new(child),
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(mut c) = handle.state::<Backend>().child.lock().unwrap().take() {
                let _ = c.kill();
                let _ = c.wait();
            }
        }
    });
}
