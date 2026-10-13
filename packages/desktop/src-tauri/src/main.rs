#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{Emitter, Manager};

/// 菜单事件通道：前端 `app/menu.ts` 监听这个事件名，载荷是菜单项 id。
const MENU_EVENT: &str = "smith://menu";

/// 原生菜单（文件 / 编辑 / 视图 / 帮助）。
///
/// 快捷键一律挂在菜单 accelerator 上，前端不再重复绑定同名键——两边都绑会
/// 双触发（菜单命中后 WebView 仍会收到按键）。除这些之外的键（Esc / Enter /
/// Ctrl+Tab / 方向键）仍由前端处理。
fn build_menu(app: &tauri::AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let file = Submenu::with_items(
        app,
        "文件",
        true,
        &[
            &MenuItem::with_id(app, "new-chat", "新建会话", true, Some("CmdOrCtrl+N"))?,
            &MenuItem::with_id(app, "close-chat", "关闭会话", true, Some("CmdOrCtrl+W"))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, None)?,
        ],
    )?;
    let edit = Submenu::with_items(
        app,
        "编辑",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    let view = Submenu::with_items(
        app,
        "视图",
        true,
        &[
            &MenuItem::with_id(app, "search", "搜索会话", true, Some("CmdOrCtrl+K"))?,
            &MenuItem::with_id(app, "toggle-sidebar", "折叠侧栏", true, Some("CmdOrCtrl+B"))?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "open-files", "文件面板", true, Some("CmdOrCtrl+P"))?,
            &MenuItem::with_id(app, "open-browser", "浏览器面板", true, Some("CmdOrCtrl+T"))?,
            &MenuItem::with_id(app, "open-terminal", "终端面板", true, Some("CmdOrCtrl+`"))?,
            &MenuItem::with_id(app, "open-diff", "更改面板", true, Some("CmdOrCtrl+D"))?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "open-settings", "设置", true, Some("CmdOrCtrl+,"))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::fullscreen(app, None)?,
        ],
    )?;
    let help = Submenu::with_items(
        app,
        "帮助",
        true,
        &[&MenuItem::with_id(app, "about", "关于 SmithCode", true, None::<&str>)?],
    )?;
    Menu::with_items(app, &[&file, &edit, &view, &help])
}

struct SidecarState {
    port: Mutex<u16>,
    /// 只活在内存里（Arc 供抽管道的线程回填）：不落盘、不进 argv。
    token: Arc<Mutex<String>>,
    child: Mutex<Option<Child>>,
    log_path: Arc<Mutex<Option<String>>>,
    /// 窗口已销毁：监督循环看到它就直接退出，不再拉起新 sidecar。
    shutting_down: AtomicBool,
}

fn default_port() -> u16 {
    18789
}

fn smith_binary() -> String {
    std::env::var("SMITH_BIN").unwrap_or_else(|_| "smith".to_string())
}

fn free_port(preferred: u16) -> u16 {
    if port_free(preferred) {
        return preferred;
    }
    if let Ok(listener) = std::net::TcpListener::bind("127.0.0.1:0") {
        if let Ok(addr) = listener.local_addr() {
            return addr.port();
        }
    }
    preferred
}

fn port_free(port: u16) -> bool {
    std::net::TcpListener::bind(format!("127.0.0.1:{port}")).is_ok()
}

fn wait_healthy(port: u16, timeout: Duration) -> bool {
    let start = std::time::Instant::now();
    while start.elapsed() < timeout {
        if http_health_ok(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

/// HTTP 级健康探针：GET `/health` 并要求 200 + `{"ok":true}`。
///
/// 纯 TCP 握手不够——端口被**任意**进程占住时 TCP 也能连上，那样前端会被
/// 指向一个陌生 listener（token 对不上 → 401 循环，甚至把 prompt 发给别人）。
/// `/health` 无需鉴权（首次启动 token 还没解析出来），但要求 serve 的真实响应体，
/// 足以把"别的进程"挡在外面。手写 HTTP/1.0 请求，不引 HTTP 客户端依赖。
fn http_health_ok(port: u16) -> bool {
    use std::io::{Read, Write};
    use std::net::TcpStream;

    let addr = match format!("127.0.0.1:{port}").parse() {
        Ok(addr) => addr,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(300)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(800)));
    // HTTP/1.0 + Connection: close：读满即结束，不用处理 chunked / keep-alive
    let request = format!(
        "GET /health HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    if stream.read_to_string(&mut buf).is_err() {
        return false;
    }
    let status_ok = buf.starts_with("HTTP/1.") && buf.contains(" 200 ");
    let body_ok = buf.contains("\"ok\"") && buf.contains("true");
    status_ok && body_ok
}

/// 日志上限：超过就轮转成 `<path>.1`（只留一份历史，防长跑把盘写满）。
const LOG_MAX_BYTES: u64 = 2 * 1024 * 1024;

fn append_log(path: &Mutex<Option<String>>, line: &str) {
    let guard = path.lock().unwrap();
    if let Some(p) = guard.as_ref() {
        // 轮转只做一次改名（覆盖旧的一份），不做多份滚动
        if let Ok(meta) = std::fs::metadata(p) {
            if meta.len() > LOG_MAX_BYTES {
                let _ = std::fs::rename(p, format!("{p}.1"));
            }
        }
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(p)
        {
            let _ = writeln!(f, "{line}");
        }
    }
}

/// sidecar 启动参数：**只含端口与工作区，绝不含 token**。
/// token 一律走 `SMITH_AUTH_TOKEN` 环境变量——argv 对同机所有用户可见
/// （`ps aux` / `/proc/<pid>/cmdline`），密钥进命令行等于公开。
///
/// 工作区必须显式传：不传时 serve 取 `os.getcwd()`，而那是 Tauri 进程的启动
/// 目录（打包后等于安装/资源目录），界面会凭空多出一个与用户无关的「项目」。
fn sidecar_args(port: u16, workspace: Option<&str>) -> Vec<String> {
    let mut args = vec!["--serve".to_string(), "--port".to_string(), port.to_string()];
    if let Some(dir) = workspace.map(str::trim).filter(|dir| !dir.is_empty()) {
        args.push("--workspace".to_string());
        args.push(dir.to_string());
    }
    args
}

/// 启动工作区：`SMITH_WORKSPACE` > 用户主目录；都拿不到（或不是目录）返回
/// `None`，由 serve 按进程 cwd 兜底（老行为）。
fn default_workspace() -> Option<String> {
    let from_env = std::env::var("SMITH_WORKSPACE").unwrap_or_default();
    let candidate = if from_env.trim().is_empty() {
        std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_default()
    } else {
        from_env
    };
    let candidate = candidate.trim();
    if candidate.is_empty() || !std::path::Path::new(candidate).is_dir() {
        return None;
    }
    Some(candidate.to_string())
}

/// 注入 serve 需要的环境变量：token 非空则显式传给 serve（重启时沿用上一次的
/// token，前端不必重新取），为空则让 serve 自己生成、从 stdout 回读。
fn apply_sidecar_env(cmd: &mut Command, token: &str) {
    // 非 tty 下 Python 默认块缓冲，不设这个 token= 行会卡在缓冲区（serve 内部另有 flush 兜底）
    cmd.env("PYTHONUNBUFFERED", "1");
    if !token.is_empty() {
        cmd.env("SMITH_AUTH_TOKEN", token);
    }
}

fn spawn_sidecar(
    app: &tauri::AppHandle,
    port: u16,
    token: &str,
    workspace: Option<&str>,
    log_path: &Mutex<Option<String>>,
) -> Option<Child> {
    use tauri_plugin_shell::ShellExt;
    // externalBin 校验：dev 下 binaries/ 可能缺对应 triple 后缀，sidecar() 会 Err，
    // 走回退链（PATH smith → python3 -m）。实际拉起统一走 "smith --serve"（参数完全可控），
    // 与 scope 的 allow 列表一致。
    if app.shell().sidecar("smith").is_err() {
        append_log(
            log_path,
            "[sidecar] externalBin smith 未随包发布，回退 PATH 中的 smith",
        );
    }
    let mut cmd = Command::new(smith_binary());
    cmd.args(sidecar_args(port, workspace));
    apply_sidecar_env(&mut cmd, token);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            append_log(
                log_path,
                &format!("[sidecar] smith 拉起失败 ({e})，回退 python3 -m smithcode"),
            );
            let mut fallback = Command::new("python3");
            fallback.args(["-m", "smithcode"]);
            fallback.args(sidecar_args(port, workspace));
            apply_sidecar_env(&mut fallback, token);
            fallback.stdout(Stdio::piped()).stderr(Stdio::piped());
            fallback.spawn().ok()
        }
    }
}

/// 抽干子进程管道（stdout/stderr 各一个线程）。
///
/// 必须抽干：serve 是长跑进程，管道写满（约 64KB）后它会卡死在 write 上。
/// stdout 里 serve 首次生成 token 时会回传一行 `token=<hex>`，顺手记进内存
/// （serve 由环境变量拿到 token 时不打印）。
fn drain_pipe(
    pipe: impl std::io::Read + Send + 'static,
    tag: &'static str,
    log_path: Arc<Mutex<Option<String>>>,
    token: Option<Arc<Mutex<String>>>,
) {
    std::thread::spawn(move || {
        for line in BufReader::new(pipe).lines().map_while(Result::ok) {
            if let Some(found) = line.trim().strip_prefix("token=") {
                if let Some(token) = token.as_ref() {
                    *token.lock().unwrap() = found.trim().to_string();
                }
                append_log(&log_path, &format!("[{tag}] token=<省略>")); // 凭据不进日志
                continue;
            }
            // 其余行也过一遍 scrub：只挡 `token=` 行不够——serve 的 stderr / 异常
            // 偶尔会把 token 或展开后的密钥原样打出来，一样会落进日志文件
            let scrubbed = match token.as_ref() {
                Some(token) => {
                    let known = token.lock().unwrap().clone();
                    if known.is_empty() {
                        line.clone()
                    } else {
                        line.replace(&known, "***")
                    }
                }
                None => line.clone(),
            };
            append_log(&log_path, &format!("[{tag}] {scrubbed}"));
        }
    });
}

/// 等 sidecar 退出，返回退出状态；子进程被 `sidecar_restart` / 关窗口取走时返回 `None`。
///
/// 关键是**不持锁等待**：`Child::wait()` 一旦在持有 `child` 锁时调用，锁会一直握到进程
/// 结束——主线程的窗口事件（点关闭按钮就是这么走的）和前端 `sidecar_restart` 全部永久
/// 阻塞，表现就是整个界面卡死、弹 "Is Not Responding"，只能 Force Quit。
/// 所以每轮只短暂拿锁 `try_wait()`，拿不到就睡一会儿再看。
fn wait_child_exit(state: &SidecarState) -> Option<ExitStatus> {
    loop {
        {
            let mut guard = state.child.lock().unwrap();
            match guard.as_mut() {
                Some(child) => match child.try_wait() {
                    Ok(Some(status)) => return Some(status),
                    Ok(None) => {} // 还在跑
                    Err(_) => return None,
                },
                None => return None, // 已被别人 take（手动重启 / 关窗口）
            }
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

fn supervise(app: tauri::AppHandle) {
    let state = app.state::<SidecarState>();
    let preferred = *state.port.lock().unwrap();
    // 不自作聪明去"复用已经在跑的 serve"：那需要把 token 落盘再带着它去验身份
    // （等于把密钥交给占住端口的任意本机进程），换来的是一个几百毫秒的省事。
    // 自己的 serve 自己起，密钥只活在内存里。
    let mut port = free_port(preferred);
    *state.port.lock().unwrap() = port;
    // 启动工作区算一次：重启沿用同一个，不因循环重算出不同值
    let workspace = default_workspace();
    let mut restarts = 0;
    loop {
        if restarts > 0 {
            port = free_port(port);
            *state.port.lock().unwrap() = port;
        }
        let token = state.token.lock().unwrap().clone();
        let app_clone = app.clone();
        let mut child = match spawn_sidecar(
            &app_clone,
            port,
            &token,
            workspace.as_deref(),
            &state.log_path,
        ) {
            Some(child) => child,
            None => {
                eprintln!("[sidecar] 启动失败：找不到 smith / python3，请先跑 smith --serve");
                return;
            }
        };
        // 两条管道各起一个线程抽干（不抽干写满即死锁）；stdout 里 serve 自己生成的
        // token 由抽管道的线程回填——首次启动 `token` 为空时就是靠它拿到凭据，
        // 前端 BootScreen 会重试到 token 同步为止。
        if let Some(stdout) = child.stdout.take() {
            drain_pipe(
                stdout,
                "stdout",
                state.log_path.clone(),
                Some(state.token.clone()),
            );
        }
        if let Some(stderr) = child.stderr.take() {
            // stderr 也带上 token：serve 的异常输出可能把 token/密钥原样打出来
            drain_pipe(
                stderr,
                "stderr",
                state.log_path.clone(),
                Some(state.token.clone()),
            );
        }
        // 入库 + 关窗竞态双检：窗口可能在 spawn 与入库**之间**被销毁，那时 Destroyed
        // 处理器拿到的是 None（还没入库），这个 child 就成了杀不掉的孤儿。这里在同一次
        // 持锁里补检：标记已置就取出来，锁外用再杀（持锁 `wait()` 会卡住主线程的窗口事件）。
        let orphan = {
            let mut guard = state.child.lock().unwrap();
            *guard = Some(child);
            if state.shutting_down.load(Ordering::SeqCst) {
                guard.take()
            } else {
                None
            }
        };
        if let Some(mut child) = orphan {
            let _ = child.kill();
            let _ = child.wait();
            append_log(
                &state.log_path,
                "[sidecar] 窗口在启动窗口期销毁，已回收刚拉起的 sidecar",
            );
            return;
        }
        let healthy_since = if wait_healthy(port, Duration::from_secs(20)) {
            Some(std::time::Instant::now())
        } else {
            eprintln!("[sidecar] 20s 内未就绪（port={port}），前端将显示等待页");
            None
        };
        let exit_status = wait_child_exit(&state);
        if state.shutting_down.load(Ordering::Relaxed) {
            append_log(&state.log_path, "[sidecar] 窗口已销毁，监督循环退出");
            return;
        }
        // 子进程槽已空 = 被 `sidecar_restart` / 关窗取走，不算崩溃：预算重置、端口沿用。
        // （不重置的话，"手动重启"也会消耗崩溃预算，崩 6 次后永久不再拉起。）
        if state.child.lock().unwrap().is_none() {
            restarts = 0;
            append_log(&state.log_path, "[sidecar] 手动重启，重启预算重置");
            std::thread::sleep(Duration::from_millis(300));
            continue;
        }
        // 跑够 60s 视为稳定运行：之后的崩溃不该和"启动即崩"累计同一份预算
        if let Some(start) = healthy_since {
            if start.elapsed() >= Duration::from_secs(60) {
                restarts = 0;
            }
        }
        append_log(
            &state.log_path,
            &format!("[sidecar] 进程退出（{exit_status:?}），准备重启"),
        );
        restarts += 1;
        if restarts > 5 {
            eprintln!("[sidecar] 重启超过 5 次，停止拉起（请检查日志后手动跑 smith --serve）");
            return;
        }
        std::thread::sleep(Duration::from_secs(2));
    }
}

fn state_dir() -> Option<String> {
    std::env::var("XDG_STATE_HOME")
        .map(|h| format!("{h}/smithcode"))
        .or_else(|_| std::env::var("HOME").map(|h| format!("{h}/.local/state/smithcode")))
        .ok()
}

fn log_path() -> Option<String> {
    state_dir().map(|d| format!("{d}/smithcode-sidecar.log"))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        // 原生目录选择器：前端「打开目录…」直接弹系统对话框
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .menu(|handle| build_menu(handle))
        .on_menu_event(|app, event| {
            // 菜单只负责发命令，具体行为在前端（App 里统一 dispatch）
            let _ = app.emit(MENU_EVENT, event.id().0.clone());
        })
        .manage(SidecarState {
            port: Mutex::new(default_port()),
            // 空 token：首次启动由 serve 自己生成，从 stdout 回读
            token: Arc::new(Mutex::new(String::new())),
            child: Mutex::new(None),
            log_path: Arc::new(Mutex::new(log_path())),
            shutting_down: AtomicBool::new(false),
        })
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || supervise(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.app_handle().state::<SidecarState>();
                // 先打标记再取子进程：监督循环看到标记就退出，不会在关闭过程中
                // 又拉起一个新 sidecar（那会留下杀不掉的孤儿进程）。
                state.shutting_down.store(true, Ordering::Relaxed);
                let child = state.child.lock().unwrap().take();
                if let Some(mut child) = child {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            sidecar_port,
            sidecar_token,
            sidecar_restart
        ])
        .run(tauri::generate_context!())
        .expect("SmithCode 启动失败");
}

#[tauri::command]
fn sidecar_port(state: tauri::State<SidecarState>) -> u16 {
    *state.port.lock().unwrap()
}

#[tauri::command]
fn sidecar_token(state: tauri::State<SidecarState>) -> String {
    state.token.lock().unwrap().clone()
}

#[tauri::command]
fn sidecar_restart(app: tauri::AppHandle) -> bool {
    let state = app.state::<SidecarState>();
    if let Some(mut child) = state.child.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
        return true;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 回归守卫：token 绝不能出现在 argv 里（`ps aux` / `/proc/<pid>/cmdline`
    /// 对同机所有用户可见），只能走环境变量。
    #[test]
    fn args_never_carry_the_token() {
        assert_eq!(sidecar_args(18789, None), vec!["--serve", "--port", "18789"]);
        assert!(sidecar_args(18789, Some("/home/me/proj"))
            .iter()
            .all(|arg| !arg.to_lowercase().contains("token")));
    }

    /// 工作区透传 `--workspace <dir>`；空白视为未设置，不产出该参数。
    #[test]
    fn workspace_flag_is_optional() {
        let absent = "--workspace".to_string();
        assert!(!sidecar_args(1, None).contains(&absent));
        assert!(!sidecar_args(1, Some("   ")).contains(&absent));
        let args = sidecar_args(1, Some("/tmp/proj"));
        let idx = args.iter().position(|a| *a == absent).expect("应带 --workspace");
        assert_eq!(args[idx + 1], "/tmp/proj");
    }

    #[test]
    fn token_goes_through_environment() {
        let mut cmd = Command::new("smith");
        apply_sidecar_env(&mut cmd, "secret-token");
        let envs: Vec<String> = cmd
            .get_envs()
            .map(|(key, value)| {
                format!(
                    "{}={}",
                    key.to_string_lossy(),
                    value.unwrap_or_default().to_string_lossy()
                )
            })
            .collect();
        assert!(envs.contains(&"SMITH_AUTH_TOKEN=secret-token".to_string()));
        assert!(envs.contains(&"PYTHONUNBUFFERED=1".to_string()));
    }

    /// 首次启动 token 为空：不设 `SMITH_AUTH_TOKEN`，由 serve 自己生成后从 stdout 回读。
    #[test]
    fn empty_token_sets_no_auth_env() {
        let mut cmd = Command::new("smith");
        apply_sidecar_env(&mut cmd, "");
        assert!(cmd
            .get_envs()
            .all(|(key, _)| key.to_string_lossy() != "SMITH_AUTH_TOKEN"));
    }

    fn test_state(child: Option<Child>) -> SidecarState {
        SidecarState {
            port: Mutex::new(0),
            token: Arc::new(Mutex::new(String::new())),
            child: Mutex::new(child),
            log_path: Arc::new(Mutex::new(None)),
            shutting_down: AtomicBool::new(false),
        }
    }

    /// 长跑子进程（跨平台，秒数参数化）：测试里用够长的时长，收尾时手动杀。
    fn child_lasting(seconds: u32) -> Child {
        #[cfg(unix)]
        let mut cmd = {
            let mut c = Command::new("sleep");
            c.arg(seconds.to_string());
            c
        };
        #[cfg(windows)]
        let mut cmd = {
            let mut c = Command::new("cmd");
            c.args(["/C", "ping", "-n", &seconds.to_string(), "127.0.0.1"]);
            c
        };
        cmd.stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("拉起长跑子进程失败")
    }

    /// 回归守卫：等待子进程时**不能**持着 `child` 锁。
    /// 持锁 `wait()` 会让主线程的窗口事件（点关闭按钮）永久阻塞——界面卡死、
    /// 弹 "Is Not Responding"、只能 Force Quit。
    ///
    /// 注意顺序：必须等等待方先进去再观察，否则观察方可能抢在它前面拿走锁，
    /// 那样即使实现是持锁等待也会"通过"（这个测试初版就是这么假通过的）。
    #[test]
    fn waiting_releases_the_child_lock() {
        let state = test_state(Some(child_lasting(30)));
        let entered = AtomicBool::new(false);
        std::thread::scope(|scope| {
            scope.spawn(|| {
                entered.store(true, Ordering::SeqCst);
                let _ = wait_child_exit(&state);
            });
            while !entered.load(Ordering::SeqCst) {
                std::thread::yield_now();
            }
            // 留出足够时间让它真的拿上锁：持锁等待时锁就此被握满整个等待期
            std::thread::sleep(Duration::from_millis(100));

            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            let mut acquired = false;
            while std::time::Instant::now() < deadline {
                if state.child.try_lock().is_ok() {
                    acquired = true;
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            assert!(
                acquired,
                "等待子进程期间 child 锁从未释放：点关闭按钮会永久卡死主线程"
            );

            // 收尾：取走并杀掉子进程，等待方随即以 None 结束
            if let Some(mut child) = state.child.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        });
    }

    /// 回归守卫：只有 TCP 握手、不响应 HTTP `/health` 的端口**不算**就绪。
    ///
    /// 纯 TCP 检查（改前的实现）会把"占住这个端口的任意进程"当成自己的 serve，
    /// 前端就被指向一个陌生 listener。这里用一个只 bind 不 accept 的监听器模拟。
    #[test]
    fn health_probe_rejects_non_http_listener() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        assert!(!http_health_ok(port), "不响应 /health 的端口必须判为未就绪");
    }

    /// 正向：真回 `200 {"ok":true}` 的端口判为就绪（保证探针本身没写死拒绝）。
    #[test]
    fn health_probe_accepts_ok_body() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = [0u8; 1024];
                let _ = sock.read(&mut buf);
                let body = b"{\"ok\": true, \"version\": \"0.0.0\"}";
                let head = format!(
                    "HTTP/1.0 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = sock.write_all(head.as_bytes());
                let _ = sock.write_all(body);
            }
        });
        assert!(http_health_ok(port));
    }
}
