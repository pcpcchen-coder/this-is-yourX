import Cocoa
import WebKit

// QRing Hand 的原生容器：視窗（WKWebView）＋選單列狀態＋內建 Python 服務的啟動與結束。
// 結構沿用 mac-app/native/Studio.swift；差別是連接埠、資料夾、選單內容。

struct MenuStatus: Decodable {
    let ring, hand, calibrated, following, busy: Bool
    let mode, label: String
}

final class HandApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backend: Process?
    var base: URL?
    var terminating = false
    let selfTest = CommandLine.arguments.contains("--self-test")
    let port = CommandLine.arguments.contains("--self-test") ? 18777 : 18775
    var startupTimer: Timer?
    var statusItem: NSStatusItem?
    var statusTimer: Timer?
    var menuStatus: MenuStatus?
    var statusRequestInFlight = false
    var commandInFlight = false
    let statusMenu = NSMenu()
    var statusLine: NSMenuItem!
    var ringItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var calibrationItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        if !selfTest {
            let menu = NSMenu()
            let appMenu = NSMenu()
            appMenu.addItem(withTitle: "結束 QRing Hand", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            let root = NSMenuItem(); root.submenu = appMenu; menu.addItem(root)
            let edit = NSMenu(title: "編輯")
            edit.addItem(withTitle: "複製", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
            edit.addItem(withTitle: "貼上", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
            edit.addItem(withTitle: "全選", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
            let editItem = NSMenuItem(title: "編輯", action: nil, keyEquivalent: ""); editItem.submenu = edit; menu.addItem(editItem)
            NSApp.mainMenu = menu
            installStatusMenu()
            let config = WKWebViewConfiguration()
            webView = WKWebView(frame: .zero, configuration: config)
            webView.navigationDelegate = self
            window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "QRing Hand · 戒指控制手掌"
            window.delegate = self
            window.isReleasedWhenClosed = false
            window.minSize = NSSize(width: 880, height: 620)
            window.contentView = webView; window.center(); window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            webView.loadHTMLString("<html><body style='background:#d9deda;color:#18211e;font:22px system-ui;padding:60px'>QRing Hand 正在啟動…</body></html>", baseURL: nil)
        }
        launchBackend()
    }
    func launchBackend() {
        let bundle = Bundle.main.bundleURL
        let resources = bundle.appendingPathComponent("Contents/Resources")
        let version = bundle.appendingPathComponent("Contents/Frameworks/Python3.framework/Versions/3.9")
        let process = Process()
        process.executableURL = version.appendingPathComponent("bin/python3.9")
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent(selfTest ? "QRing Hand SelfTest" : "QRing Hand")
        var arguments = ["-u", resources.appendingPathComponent("app/server.py").path, "--port", String(port), "--data-dir", support.path, "--watch-parent"]
        if selfTest { arguments.append("--self-test") }
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONHOME"] = version.path
        environment["PYTHONPATH"] = resources.appendingPathComponent("app").path + ":" + resources.appendingPathComponent("site-packages").path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.removeValue(forKey: "VIRTUAL_ENV")
        process.environment = environment
        let output = Pipe(); process.standardOutput = output
        let error = Pipe(); process.standardError = error
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            let string = String(data: data, encoding: .utf8) ?? ""
            if self.selfTest { FileHandle.standardOutput.write(data) }
            if string.contains("READY http://127.0.0.1:") {
                DispatchQueue.main.async { self.ready() }
            }
        }
        error.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            if self.selfTest { FileHandle.standardError.write(data) }
        }
        process.terminationHandler = { p in
            DispatchQueue.main.async {
                if self.terminating { return }
                self.startupTimer?.invalidate()
                if self.selfTest { exit(2) }
                self.menuStatus = nil
                self.updateStatusMenu()
                self.webView.loadHTMLString("<html><body style='background:#d9deda;color:#18211e;font:18px system-ui;padding:40px'>內建服務無法啟動或已結束。請確認沒有另一個 QRing Hand 正在執行，再重新開啟。系統代碼 \(p.terminationStatus)。</body></html>", baseURL: nil)
            }
        }
        backend = process
        do { try process.run() }
        catch { if selfTest { print(error); exit(2) }; showError("無法啟動內建服務：\(error.localizedDescription)") }
        startupTimer = Timer.scheduledTimer(withTimeInterval: 20, repeats: false) { _ in
            if self.base == nil {
                if self.selfTest { print("STARTUP_TIMEOUT"); self.backend?.terminate(); exit(2) }
                self.showError("服務啟動逾時。請確認沒有另一個 QRing Hand 正在執行。")
            }
        }
    }
    func ready() {
        guard base == nil else { return }
        startupTimer?.invalidate()
        base = URL(string: "http://127.0.0.1:\(port)/")!
        if selfTest {
            URLSession.shared.dataTask(with: base!.appendingPathComponent("api/state")) { data, _, error in
                guard error == nil, let data = data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any], json["options"] != nil else { print("SELF_TEST_FAILED"); exit(2) }
                URLSession.shared.dataTask(with: self.base!.appendingPathComponent("api/menu-status")) { data, response, error in
                    guard error == nil, (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                          let status = try? JSONDecoder().decode(MenuStatus.self, from: data),
                          !status.ring, !status.hand, !status.following, status.mode == "disconnected" else { print("STATUS_SELF_TEST_FAILED"); exit(2) }
                    print("BUNDLED_APP_SELF_TEST_OK")
                    self.shutdown { exit(0) }
                }.resume()
            }.resume()
        } else {
            webView.load(URLRequest(url: base!))
            refreshMenuStatus()
            let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in self?.refreshMenuStatus() }
            RunLoop.main.add(timer, forMode: .common)
            statusTimer = timer
        }
    }
    func menuItem(_ title: String, action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        statusMenu.addItem(item)
        return item
    }
    func installStatusMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusMenu.autoenablesItems = false
        statusLine = NSMenuItem(title: "QRing Hand 正在啟動…", action: nil, keyEquivalent: "")
        statusLine.isEnabled = false
        statusMenu.addItem(statusLine)
        statusMenu.addItem(.separator())
        _ = menuItem("顯示 QRing Hand", action: #selector(showWindow))
        calibrationItem = menuItem("開啟校正", action: #selector(showCalibration))
        statusMenu.addItem(.separator())
        stopItem = menuItem("停止（關掉手掌扭力）", action: #selector(stopFollow))
        statusMenu.addItem(.separator())
        ringItem = menuItem("連接戒指", action: #selector(toggleRing))
        statusMenu.addItem(.separator())
        _ = menuItem("結束 QRing Hand", action: #selector(quitApp))
        statusItem?.menu = statusMenu
        updateStatusMenu()
    }
    func updateStatusMenu() {
        guard let button = statusItem?.button else { return }
        let s = menuStatus
        let label = s?.label ?? (base == nil ? "啟動中" : "服務離線")
        let symbol: String
        switch s?.mode {
        case "following": symbol = "hand.raised.fill"
        case "ready": symbol = "hand.raised"
        case "busy", "calibrating": symbol = "hourglass"
        case "fault": symbol = "exclamationmark.triangle"
        default: symbol = "circle.dashed"
        }
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "QRing Hand：\(label)") ?? NSImage(systemSymbolName: "hand.raised", accessibilityDescription: "QRing Hand")
        image?.isTemplate = true
        button.image = image
        button.imagePosition = .imageLeading
        button.title = " \(label)"
        button.toolTip = "QRing Hand · \(label)"
        button.setAccessibilityLabel("QRing Hand · \(label)")
        statusLine.title = "手掌跟隨：\(label)"
        let available = s != nil && !terminating && !commandInFlight
        ringItem.title = s?.ring == true ? "中斷戒指" : "連接戒指"
        ringItem.isEnabled = available
        stopItem.isEnabled = available && (s?.hand == true || s?.following == true)
        calibrationItem.isEnabled = available
    }
    func refreshMenuStatus() {
        guard let base = base, !terminating, !statusRequestInFlight else { return }
        statusRequestInFlight = true
        var request = URLRequest(url: base.appendingPathComponent("api/menu-status"))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            let status: MenuStatus?
            if error == nil, (response as? HTTPURLResponse)?.statusCode == 200, let data = data { status = try? JSONDecoder().decode(MenuStatus.self, from: data) }
            else { status = nil }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.statusRequestInFlight = false
                guard !self.terminating else { return }
                self.menuStatus = status
                self.updateStatusMenu()
            }
        }.resume()
    }
    func command(_ path: String, body: [String: Any] = [:]) {
        guard let base = base, !terminating, !commandInFlight else { return }
        commandInFlight = true
        updateStatusMenu()
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        request.timeoutInterval = 60
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            var message = error?.localizedDescription
            if error == nil && (response as? HTTPURLResponse)?.statusCode != 200 {
                let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                message = json?["error"] as? String ?? "操作失敗，請在主視窗查看狀態。"
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.commandInFlight = false
                guard !self.terminating else { return }
                self.updateStatusMenu()
                self.refreshMenuStatus()
                if let message = message { self.showWindow(); self.showError(message) }
            }
        }.resume()
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func showCalibration() { showWindow(); webView.evaluateJavaScript("switchTab('calibration')", completionHandler: nil) }
    @objc func stopFollow() { command("api/follow/stop") }
    @objc func toggleRing() { guard let s = menuStatus else { return }; command(s.ring ? "api/ring/disconnect" : "api/ring/connect") }
    @objc func quitApp() { NSApp.terminate(nil) }
    func windowShouldClose(_ sender: NSWindow) -> Bool { sender.orderOut(nil); return false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func showError(_ text: String) { let alert = NSAlert(); alert.messageText = "QRing Hand"; alert.informativeText = text; alert.runModal() }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if url.scheme == "about" || (url.host == "127.0.0.1" && url.port == port) { decisionHandler(.allow) }
        else { decisionHandler(.cancel) }
    }
    func shutdown(_ completion: @escaping () -> Void) {
        terminating = true
        statusTimer?.invalidate()
        updateStatusMenu()
        guard let base = base else { backend?.terminate(); completion(); return }
        var request = URLRequest(url: base.appendingPathComponent("api/shutdown"))
        request.httpMethod = "POST"; request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = Data("{}".utf8); request.timeoutInterval = 60
        URLSession.shared.dataTask(with: request) { _, _, _ in
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                if self.backend?.isRunning == true { self.backend?.terminate() }
                completion()
            }
        }.resume()
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating { return .terminateNow }
        shutdown { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

let app = NSApplication.shared
let handApp = HandApp()
app.delegate = handApp
app.setActivationPolicy(handApp.selfTest ? .prohibited : .regular)
app.run()
