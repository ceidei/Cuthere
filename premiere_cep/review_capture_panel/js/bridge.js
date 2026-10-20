// bridge.js — command bridge inside the Cuthere panel.
//
// Runs inside Adobe Premiere Pro CEP panel.
// Polls the bridge directory for command JSON files from Hermes,
// executes them via ExtendScript (hostscript.jsx),
// and writes response JSON files back.
//
// Bridge directory: C:\Users\Public\Documents\ave-bridge\
//   commands/   ← Hermes writes command .json files here
//   responses/  ← Panel writes response .json files here
//   locks/      ← Atomicity guards

(function () {
    "use strict";

    // ═══════════════════════════════════════════════════════════════════
    // Configuration (auto-detected by platform)
    // ═══════════════════════════════════════════════════════════════════

    var BRIDGE_DIR = "";
    var POLL_INTERVAL_MS = 500;
    var STATUS_UPDATE_MS = 5000;

    function getBridgeDir() {
        var extensionPath = csInterface.getSystemPath(SystemPath.EXTENSION).replace(/\\/g, "/");
        var raw = fsReadFile(extensionPath + "/runtime.json");
        if (raw) {
            var config = JSON.parse(raw);
            if (typeof config.bridge_dir === "string" && config.bridge_dir) return config.bridge_dir;
        }
        var platform = navigator.platform || "";
        if (platform.indexOf("Mac") >= 0) {
            return "/Users/Shared/ave-bridge";
        }
        return "C:\\Users\\Public\\Documents\\ave-bridge";
    }

    function getSep() {
        return (navigator.platform || "").indexOf("Mac") >= 0 ? "/" : "\\";
    }

    function joinPath(base, sub) {
        return base + getSep() + sub;
    }

    // ═══════════════════════════════════════════════════════════════════
    // Filesystem helpers (using CEP cep.fs API)
    // ═══════════════════════════════════════════════════════════════════

    function fsExists(path) {
        try {
            var result = cep.fs.stat(path);
            return result && result.err === 0;
        } catch (e) {
            return false;
        }
    }

    function fsMkdir(path) {
        if (fsExists(path)) return;
        var result = cep.fs.makedir(path);
        if ((!result || result.err !== 0) && !fsExists(path)) throw new Error("Cannot create " + path);
    }

    function fsReaddir(path) {
        try {
            var r = cep.fs.readdir(path);
            if (r && r.err === 0 && r.data) return r.data;
            return [];
        } catch (e) {
            return [];
        }
    }

    // FIX (2026-10-17): cep.fs.readFile returns {err, data}; return the string content only.
    function fsReadFile(path) {
        try {
            var r = cep.fs.readFile(path);
            if (r && r.err === 0 && typeof r.data === "string") return r.data;
            return null;
        } catch (e) {
            return null;
        }
    }

    function fsWriteFile(path, data) {
        var result = cep.fs.writeFile(path, data);
        if (!result || result.err !== 0) throw new Error("Write failed: " + path);
    }

    function fsUnlink(path) {
        try {
            cep.fs.deleteFile(path);
        } catch (e) {
            // File may already be gone
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // State
    // ═══════════════════════════════════════════════════════════════════

    var csInterface = null;
    var isRunning = false;
    var pollTimer = null;
    var statusTimer = null;
    var stats = {
        commands_processed: 0,
        errors: 0,
        last_command: "",
        last_time: null,
    };

    // Track already-seen request IDs to avoid double-processing
    var seenRequests = {};
    var commandInFlight = false;
    var hostReady = false;
    var hostLoading = false;

    // ═══════════════════════════════════════════════════════════════════
    // DOM References
    // ═══════════════════════════════════════════════════════════════════

    function $(id) { return document.getElementById(id); }

    var elStatus = null;
    var elBridgeDir = null;
    var elPremiereStatus = null;
    var elCommandsProcessed = null;
    var elErrors = null;
    var elLastCommand = null;
    var elLog = null;
    var elStartBtn = null;
    var elStopBtn = null;

    // ═══════════════════════════════════════════════════════════════════
    // Initialization
    // ═══════════════════════════════════════════════════════════════════

    function init() {
        csInterface = new CSInterface();

        // Cache DOM elements
        elStatus = $("bridge-status");
        elBridgeDir = $("bridge-dir");
        elPremiereStatus = $("premiere-status");
        elCommandsProcessed = $("commands-processed");
        elErrors = $("errors");
        elLastCommand = $("last-command");
        elLog = $("log");
        elStartBtn = $("start-btn");
        elStopBtn = $("stop-btn");

        // Button handlers
        elStartBtn.addEventListener("click", ensureHostAndStart);
        elStopBtn.addEventListener("click", stopBridge);
        window.addEventListener("beforeunload", stopBridge);
        ensureHostAndStart();
    }

    function ensureHostAndStart() {
        if (isRunning || hostLoading) return;
        if (hostReady) { startBridge(); return; }
        try {
            BRIDGE_DIR = getBridgeDir();
            elBridgeDir.textContent = BRIDGE_DIR;
            checkDirectories();
        } catch (error) {
            log(String(error));
            updateStatus("error", "连接失败，请查看诊断");
            return;
        }
        hostLoading = true;
        elStartBtn.disabled = true;

        // Auto-start
        log("Cuthere CEP Bridge initialized");
        log("Bridge directory: " + BRIDGE_DIR);
        // $.fileName is not reliable for a manifest-loaded ExtendScript.
        // Load both host files explicitly and start polling only after success.
        var extensionPath = csInterface.getSystemPath(SystemPath.EXTENSION).replace(/\\/g, "/");
        var loadScript = 'try { $.evalFile(' + JSON.stringify(extensionPath + '/js/hostscript.jsx') + '); ' +
            '$.evalFile(' + JSON.stringify(extensionPath + '/js/project_lifecycle.jsx') + '); ping("{}"); ' +
            '} catch(e) { "LOAD_ERROR: " + e.toString() + " line=" + e.line; }';
        csInterface.evalScript(loadScript, function (result) {
            hostLoading = false;
            elStartBtn.disabled = false;
            try {
                var health = JSON.parse(result);
                if (!health.success || health.data.version !== "1.1.0") throw new Error(result);
                hostReady = true;
                log("Host 1.1.0 loaded");
                startBridge();
            } catch (e) {
                log("Host load failed: " + result);
                updateStatus("error", "连接失败，请查看诊断");
            }
        });
    }

    // ═══════════════════════════════════════════════════════════════════
    // Bridge Control
    // ═══════════════════════════════════════════════════════════════════

    function startBridge() {
        if (isRunning) return;
        if (!hostReady) return;

        isRunning = true;
        updateStatus("running", "已就绪");
        checkPremiereConnection();
        elStartBtn.disabled = true;
        elStopBtn.disabled = false;
        log("Bridge started — polling every " + POLL_INTERVAL_MS + "ms");

        // Start polling loop
        pollLoop();

        // Start status check loop
        statusLoop();

        // Immediate first poll
        setTimeout(processCommands, 100);
    }

    function stopBridge() {
        isRunning = false;
        updateStatus("stopped", "已暂停");
        elStartBtn.disabled = false;
        elStopBtn.disabled = true;

        if (pollTimer) clearTimeout(pollTimer);
        if (statusTimer) clearInterval(statusTimer);
        pollTimer = null;
        statusTimer = null;

        log("Bridge stopped");
    }

    // ═══════════════════════════════════════════════════════════════════
    // Main Poll Loop
    // ═══════════════════════════════════════════════════════════════════

    function pollLoop() {
        if (!isRunning) return;
        processCommands();
        pollTimer = setTimeout(pollLoop, POLL_INTERVAL_MS);
    }

    function statusLoop() {
        statusTimer = setInterval(function () {
            if (isRunning) {
                checkPremiereConnection();
            }
        }, STATUS_UPDATE_MS);
    }

    // ═══════════════════════════════════════════════════════════════════
    // Command Processing
    // ═══════════════════════════════════════════════════════════════════

    function processCommands() {
        if (!isRunning || commandInFlight) return;
        var commandsDir = joinPath(BRIDGE_DIR, "commands");
        var responsesDir = joinPath(BRIDGE_DIR, "responses");
        var sep = getSep();

        try {
            var files = fsReaddir(commandsDir);
            var jsonFiles = [];
            for (var i = 0; i < files.length; i++) {
                if (/\.json$/i.test(files[i]) && !(files[i] in seenRequests)) {
                    jsonFiles.push(files[i]);
                }
            }

            if (jsonFiles.length === 0) return;

            // Sort by filename (timestamp-based)
            jsonFiles.sort();

            // Exactly one ExtendScript mutation at a time, including across polls.
            processCommandFile(jsonFiles[0], commandsDir + sep, responsesDir + sep);
        } catch (e) {
            log("Error scanning commands: " + e.toString());
            stats.errors++;
            updateStats();
        }
    }

    function processCommandFile(filename, commandsDir, responsesDir) {
        var requestId = "";
        try {
            var filePath = commandsDir + filename;
            var content = fsReadFile(filePath);
            var cmd = JSON.parse(content);
            requestId = cmd.request_id;
            if (!/^[A-Za-z0-9_-]+$/.test(requestId || "") || filename !== requestId + ".json") {
                requestId = "";
                throw new Error("Invalid request_id or filename");
            }
            if (cmd.expires_at && Date.now() > cmd.expires_at * 1000) {
                throw new Error("Request expired before execution; no change was made");
            }
            var command = cmd.command;
            var params = JSON.stringify(cmd.params || {});

            seenRequests[filename] = true;
            commandInFlight = true;
            log("Processing: " + command + " [" + requestId + "]");
            stats.last_command = command;
            stats.last_time = new Date().toLocaleTimeString();

            // Execute via ExtendScript
            var script = 'dispatchBridgeRequest(' + JSON.stringify(requestId) + ', ' +
                JSON.stringify(command) + ', ' + JSON.stringify(params) + ', ' + JSON.stringify(BRIDGE_DIR) + ');';

            csInterface.evalScript(script, function (result) {
                try {
                    var parsed = JSON.parse(result);

                    // The host writes the atomic response before returning.
                    // This callback may disappear when a project switch reloads CEP.

                    stats.commands_processed++;
                    log("  -> " + (parsed.success ? "OK" : "FAILED: " + (parsed.error || "unknown")));

                    // Delete command file
                    fsUnlink(filePath);
                    delete seenRequests[filename];
                } catch (e) {
                    stats.errors++;
                    log("  -> Response parse error: " + e.toString());
                    // Never overwrite a durable host result with a callback error.
                } finally {
                    fsUnlink(filePath);
                    delete seenRequests[filename];
                    commandInFlight = false;
                }
                updateStats();
            });

        } catch (e) {
            commandInFlight = false;
            delete seenRequests[filename];
            stats.errors++;
            log("Error processing command " + requestId + ": " + e.toString());
            updateStats();

            // Write error response
            try {
                if (requestId) fsWriteFile(responsesDir + requestId + ".json", JSON.stringify({
                    request_id: requestId,
                    success: false,
                    error: e.toString(),
                    timestamp: Date.now(),
                }));
                fsUnlink(commandsDir + filename);
            } catch (e2) {
                // Best effort
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Health Checks
    // ═══════════════════════════════════════════════════════════════════

    function checkDirectories() {
        var sep = getSep();
        var cmdDir = joinPath(BRIDGE_DIR, "commands") + sep;
        var respDir = joinPath(BRIDGE_DIR, "responses") + sep;
        var lockDir = joinPath(BRIDGE_DIR, "locks") + sep;

        fsMkdir(BRIDGE_DIR);
        fsMkdir(cmdDir);
        fsMkdir(respDir);
        fsMkdir(lockDir);

        if (!fsExists(cmdDir) || !fsExists(respDir)) {
            log("ERROR: Cannot create bridge directories");
            throw new Error("Cannot access bridge directory");
        } else {
            log("Bridge directories OK");
        }
    }

    function checkPremiereConnection() {
        if (!isRunning || commandInFlight) return;

        csInterface.evalScript('ping("{}")', function (result) {
            try {
                var parsed = JSON.parse(result);
                if (parsed.success) {
                    elPremiereStatus.textContent = "已连接";
                    elPremiereStatus.className = "status-ok";
                } else {
                    elPremiereStatus.textContent = "连接异常：" + (parsed.error || "unknown");
                    elPremiereStatus.className = "status-error";
                }
            } catch (e) {
                elPremiereStatus.textContent = "无响应";
                elPremiereStatus.className = "status-warn";
            }
        });
    }

    // ═══════════════════════════════════════════════════════════════════
    // UI Helpers
    // ═══════════════════════════════════════════════════════════════════

    function updateStatus(state, message) {
        elStatus.textContent = message;
        elStatus.className = "status-" + state;
    }

    function updateStats() {
        elCommandsProcessed.textContent = stats.commands_processed;
        elErrors.textContent = stats.errors;
        elLastCommand.textContent = stats.last_command || "—";
    }

    function log(message) {
        var time = new Date().toLocaleTimeString();
        var line = "[" + time + "] " + message + "\n";
        elLog.textContent += line;
        elLog.scrollTop = elLog.scrollHeight;

        // Also log to CEP console
        if (csInterface) {
            try {
                csInterface.log(line.trim());
            } catch (e) {}
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    // Startup
    // ═══════════════════════════════════════════════════════════════════

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

})();
