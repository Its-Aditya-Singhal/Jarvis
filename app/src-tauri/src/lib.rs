//! Desktop shell: starts the local Python backend on a random loopback port
//! with a per-launch API token, and hands both to the UI.
//!
//! The packaged app carries the backend as a PyInstaller folder in
//! `Contents/Resources/backend/` (see scripts/build_dmg.sh); a development
//! checkout runs `backend/.venv/bin/python -m jarvis` instead.
//!
//! If the backend stops on its own (a crash, or macOS ending it when memory runs
//! short) it is started again on the same port and token, so the UI simply
//! reconnects. A backend that keeps stopping is given up on, and the boot screen
//! says so.

use std::collections::VecDeque;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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
    /// set when the app is quitting: a backend that exits now is not restarted
    quitting: AtomicBool,
}

/// Restarts allowed within `RESTART_WINDOW`; a backend that dies straight after
/// starting (a broken install) would otherwise be restarted forever.
const MAX_RESTARTS: usize = 3;
const RESTART_WINDOW: Duration = Duration::from_secs(120);

/// Remembers recent restarts and says whether another one is allowed.
struct RestartBudget {
    recent: VecDeque<Instant>,
    max: usize,
    window: Duration,
}

impl RestartBudget {
    fn new(max: usize, window: Duration) -> Self {
        Self {
            recent: VecDeque::new(),
            max,
            window,
        }
    }

    fn allow(&mut self, now: Instant) -> bool {
        while let Some(&t) = self.recent.front() {
            if now.duration_since(t) > self.window {
                self.recent.pop_front();
            } else {
                break;
            }
        }
        if self.recent.len() >= self.max {
            return false;
        }
        self.recent.push_back(now);
        true
    }
}

#[tauri::command]
fn backend_info(state: tauri::State<Arc<Backend>>) -> BackendInfo {
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

/// Watches the backend and starts it again when it stops on its own.
/// `make` builds the command for each (re)start.
fn supervise(state: Arc<Backend>, make: impl Fn() -> Command) {
    let mut budget = RestartBudget::new(MAX_RESTARTS, RESTART_WINDOW);
    loop {
        std::thread::sleep(Duration::from_millis(500));
        if state.quitting.load(Ordering::SeqCst) {
            return;
        }
        let exit = {
            let mut child = state.child.lock().unwrap();
            match child.as_mut().map(|c| c.try_wait()) {
                None => return, // taken by the quit handler
                Some(Ok(Some(status))) => {
                    *child = None;
                    status
                }
                Some(Ok(None)) => continue,
                Some(Err(e)) => {
                    eprintln!("can't check on the backend: {e}");
                    continue;
                }
            }
        };
        if state.quitting.load(Ordering::SeqCst) {
            return;
        }
        eprintln!("backend stopped ({exit})");
        if !budget.allow(Instant::now()) {
            state.info.lock().unwrap().error = Some(format!(
                "The assistant's engine keeps stopping ({exit}). Quit and reopen the app."
            ));
            return;
        }
        std::thread::sleep(Duration::from_secs(1)); // let the old process release its port
        let info = state.info.lock().unwrap().clone();
        // holding the lock while starting: the quit handler then waits and stops this one too
        let mut child = state.child.lock().unwrap();
        if state.quitting.load(Ordering::SeqCst) {
            return;
        }
        match spawn_backend(make(), &info) {
            Ok(c) => *child = Some(c),
            Err(e) => {
                state.info.lock().unwrap().error =
                    Some(format!("The assistant's engine could not restart: {e}"));
                return;
            }
        }
    }
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
        .manage(Arc::new(Backend {
            info: Mutex::new(info),
            child: Mutex::new(None),
            quitting: AtomicBool::new(false),
        }))
        .setup(|app| {
            let state = app.state::<Arc<Backend>>().inner().clone();
            let resources = app.path().resource_dir().ok();
            let data = app.path().app_data_dir().ok();
            let make = move || backend_command(resources.clone(), data.clone());
            let info = state.info.lock().unwrap().clone();
            match spawn_backend(make(), &info) {
                Ok(c) => {
                    *state.child.lock().unwrap() = Some(c);
                    std::thread::Builder::new()
                        .name("backend-supervisor".into())
                        .spawn(move || supervise(state, make))?;
                }
                Err(e) => {
                    eprintln!("failed to start backend: {e}");
                    state.info.lock().unwrap().error =
                        Some(format!("The assistant's engine could not start: {e}"));
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![backend_info])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            let state = handle.state::<Arc<Backend>>();
            state.quitting.store(true, Ordering::SeqCst);
            let child = state.child.lock().unwrap().take();
            if let Some(mut c) = child {
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restarts_are_limited_within_the_window() {
        let mut b = RestartBudget::new(3, Duration::from_secs(120));
        let t0 = Instant::now();
        assert!(b.allow(t0));
        assert!(b.allow(t0 + Duration::from_secs(1)));
        assert!(b.allow(t0 + Duration::from_secs(2)));
        assert!(!b.allow(t0 + Duration::from_secs(3)));
        // the first restart has left the window: one more is allowed
        assert!(b.allow(t0 + Duration::from_secs(121)));
        assert!(!b.allow(t0 + Duration::from_secs(121)));
    }

    #[cfg(unix)]
    fn state_with(child: Child) -> Arc<Backend> {
        Arc::new(Backend {
            info: Mutex::new(BackendInfo {
                port: 1,
                token: "t".into(),
                error: None,
            }),
            child: Mutex::new(Some(child)),
            quitting: AtomicBool::new(false),
        })
    }

    #[cfg(unix)]
    #[test]
    fn a_backend_that_keeps_dying_is_restarted_then_given_up_on() {
        use std::sync::atomic::AtomicUsize;
        let starts = Arc::new(AtomicUsize::new(0));
        let n = starts.clone();
        let state = state_with(Command::new("true").spawn().unwrap());
        supervise(state.clone(), move || {
            n.fetch_add(1, Ordering::SeqCst);
            Command::new("true")
        });
        assert_eq!(starts.load(Ordering::SeqCst), MAX_RESTARTS);
        let error = state.info.lock().unwrap().error.clone().unwrap();
        assert!(error.contains("keeps stopping"), "{error}");
    }

    #[cfg(unix)]
    #[test]
    fn nothing_is_restarted_while_quitting() {
        let state = state_with(Command::new("true").spawn().unwrap());
        state.quitting.store(true, Ordering::SeqCst);
        supervise(state.clone(), || panic!("restarted while quitting"));
        assert!(state.info.lock().unwrap().error.is_none());
    }
}
