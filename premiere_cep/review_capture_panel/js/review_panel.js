(function () {
    "use strict";

    var cs = null;
    var extensionRoot = "";
    var appRoot = "";
    var ipcRoot = "";
    var runtimeConfig = null;
    var workerSpawned = false;
    var visibilityLaunched = false;
    var projectPath = "";
    var sessionId = "";
    var active = null;
    var devices = [];
    var showAllDevices = false;
    var previewTimer = null;
    var contextRefreshing = false;
    var statusTimer = null;
    var heartbeatTimer = null;
    var transcriptionTimer = null;
    var transcriptionQueue = [];
    var transcriptionActive = null;
    var transcriptionFailed = {};
    var transcriptDrafts = {};
    var currentNotes = [];
    var transcriptionStartedAt = 0;
    var transcriptionJobSeen = false;
    var busy = false;
    var pttCurrent = null;
    var pttHandledId = "";
    var pttHotkey = "F8";

    function $(id) { return document.getElementById(id); }
    function join(a, b) { return String(a).replace(/[\\/]+$/, "") + "\\" + String(b).replace(/^[\\/]+/, "").replace(/[\\/]/g, "\\"); }
    function guid() {
        return "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".replace(/x/g, function () { return Math.floor(Math.random() * 16).toString(16); });
    }
    function showNotice(text, kind) {
        var el = $("notice"); el.textContent = text || ""; el.className = "notice" + (kind ? " " + kind : "");
    }
    function resultError(result, label) {
        if (!result || result.err !== 0) {
            throw new Error(label + (result && result.err !== undefined ? "（错误码 " + result.err + "）" : ""));
        }
        return result.data;
    }
    function mkdir(path) {
        var status = window.cep.fs.stat(path);
        if (status && status.err === 0) { return; }
        var result = window.cep.fs.makedir(path);
        if (!result || result.err !== 0) {
            status = window.cep.fs.stat(path);
            if (!status || status.err !== 0) { throw new Error("不能创建目录：" + path + "（" + (result ? result.err : "unknown") + "）"); }
        }
    }
    function ensureDirs() {
        mkdir(appRoot);
        mkdir(ipcRoot);
        ["inbox", "outbox", "processing", "results", "recover"].forEach(function (name) { mkdir(join(ipcRoot, name)); });
    }
    function writeAtomic(path, value) {
        var temporary = path + "." + guid() + ".partial";
        resultError(window.cep.fs.writeFile(temporary, typeof value === "string" ? value : JSON.stringify(value)), "写文件失败");
        resultError(window.cep.fs.rename(temporary, path), "提交文件失败");
    }
    function readJson(path) {
        var result = window.cep.fs.readFile(path);
        if (!result || result.err !== 0 || !result.data) { return null; }
        try { return JSON.parse(result.data); } catch (e) { return null; }
    }
    function removeFile(path) {
        try { var result = window.cep.fs.deleteFile(path); return !result || result.err === 0; } catch (e) { return false; }
    }
    function setWorkerStatus(text, state) {
        var el = $("worker-status"); el.textContent = text; el.className = "pill" + (state ? " " + state : "");
    }
    function parseHostResult(raw) {
        if (!raw || raw === "EvalScript error.") { throw new Error("Premiere 宿主没有返回结果（" + String(raw) + "）"); }
        var value = JSON.parse(raw);
        if (!value.ok) { throw new Error(value.error || "读取 Premiere 失败"); }
        return value.data;
    }
    function hostCall(method, arg, callback) {
        var suffix = arg === undefined ? "" : "(" + JSON.stringify(arg) + ")";
        var hostScript = join(join(extensionRoot, "js"), "review_host.jsx").replace(/\\/g, "/");
        var call = '(function () { try { ' +
            'if (typeof $._AECapture === "undefined" || $._AECapture.__reviewBridgeVersion !== "1.2.6" || typeof $._AECapture.' + method + ' !== "function") ' +
            '{ $.evalFile(File(' + JSON.stringify(hostScript) + ')); } ' +
            'return $._AECapture.' + method + (arg === undefined ? "()" : suffix) + '; ' +
            '} catch (e) { return "{\\"ok\\":false,\\"error\\":\\"Premiere 脚本加载失败\\"}"; } }())';
        cs.evalScript(call, function (raw) {
            if (!raw || raw === "EvalScript error.") {
                try {
                    window.cep.fs.writeFile(join(ipcRoot, "host_last_error.json"), JSON.stringify({
                        at: new Date().toISOString(), method: method, raw: String(raw),
                        script_length: call.length, script_prefix: call.slice(0, 240)
                    }));
                } catch (ignored) {}
            }
            try { callback(null, parseHostResult(raw)); }
            catch (error) { callback(error); }
        });
    }
    function probeHostBridge() {
        var probes = [
            { name: "arithmetic", script: "1+1" },
            { name: "json", script: "typeof JSON" },
            { name: "project", script: "app.project ? String(app.project.path) : 'no-project'" },
            { name: "adapter", script: "typeof $._AECapture" }
        ];
        var results = [];
        function next() {
            if (!probes.length) {
                try { window.cep.fs.writeFile(join(ipcRoot, "host_probe.json"), JSON.stringify({ at: new Date().toISOString(), results: results })); }
                catch (ignored) {}
                return;
            }
            var item = probes.shift();
            cs.evalScript(item.script, function (raw) { results.push({ name: item.name, raw: String(raw) }); next(); });
        }
        next();
    }
    function runtimePath() { return join(extensionRoot, "runtime.json"); }
    function statusPath() { return join(ipcRoot, "status.json"); }
    function workerIsOnline() {
        var status = readJson(statusPath());
        if (!status || !status.updated_at) { return false; }
        var time = Date.parse(status.updated_at);
        return isFinite(time) && (Date.now() - time) < 2500;
    }
    function launchWorker(callback) {
        runtimeConfig = readJson(runtimePath());
        if (!runtimeConfig || !runtimeConfig.pythonw || !runtimeConfig.worker_entry || !runtimeConfig.ipc_root) {
            callback(new Error("没有找到录音助手配置；请先运行面板安装脚本")); return;
        }
        ipcRoot = runtimeConfig.ipc_root;
        appRoot = ipcRoot.replace(/[\\/]+ipc(?:[\\/][^\\/]+)?[\\/]*$/i, "");
        ensureDirs();
        if (!visibilityLaunched && runtimeConfig.visibility_entry && window.cep.process && typeof window.cep.process.createProcess === "function") {
            var visibilityLaunch = window.cep.process.createProcess(runtimeConfig.pythonw, runtimeConfig.visibility_entry, "--ipc-root", ipcRoot);
            visibilityLaunched = !!(visibilityLaunch && visibilityLaunch.err === 0);
        }
        if (workerIsOnline()) { callback(null); return; }
        if (workerSpawned) { callback(new Error("录音助手没有启动；请检查安装状态")); return; }
        if (!window.cep.process || typeof window.cep.process.createProcess !== "function") {
            callback(new Error("当前 CEP 环境没有启动本机录音助手的接口")); return;
        }
        workerSpawned = true;
        var processArgs = [runtimeConfig.pythonw, runtimeConfig.worker_entry, "--ipc-root", ipcRoot];
        var launch = window.cep.process.createProcess.apply(window.cep.process, processArgs);
        if (!launch || launch.err !== 0) {
            workerSpawned = false;
            callback(new Error("启动录音助手失败" + (launch && launch.err !== undefined ? "（错误码 " + launch.err + "）" : "")));
            return;
        }
        var started = Date.now();
        var timer = setInterval(function () {
            if (workerIsOnline()) { clearInterval(timer); callback(null); }
            else if (Date.now() - started > 8000) { clearInterval(timer); workerSpawned = false; callback(new Error("8 秒内没有收到录音助手回应")); }
        }, 200);
    }
    function sendRequest(action, payload, callback, timeoutMs) {
        launchWorker(function (launchError) {
            if (launchError) { callback(launchError); return; }
            var requestId = ("0000000000000" + Date.now()).slice(-13) + guid();
            var command = { request_id: requestId, action: action, payload: payload || {}, created_at: new Date().toISOString() };
            var commandPath = join(join(ipcRoot, "inbox"), requestId + ".json");
            var replyPath = join(join(ipcRoot, "outbox"), requestId + ".json");
            try { writeAtomic(commandPath, command); }
            catch (error) { callback(error); return; }
            var started = Date.now();
            var notifiedPending = false;
            var timer = setInterval(function () {
                var reply = readJson(replyPath);
                if (reply) {
                    clearInterval(timer);
                    removeFile(replyPath);
                    if (!reply.ok) { callback(new Error(reply.message || reply.error || "录音助手请求失败"), reply, notifiedPending); }
                    else { callback(null, reply.result, notifiedPending); }
                } else if (!notifiedPending && Date.now() - started > (timeoutMs || 12000)) {
                    notifiedPending = true;
                    callback(new Error("录音助手尚未返回结果；正在继续等待，请不要重复点击"), null, false);
                } else if (notifiedPending && Date.now() - started > ((timeoutMs || 12000) + 120000)) {
                    clearInterval(timer);
                    callback(new Error("录音助手仍未返回结果；请刷新意见清单并核对状态"), null, false);
                }
            }, 100);
        });
    }
    function heartbeat() {
        try {
            var heartbeatPath = join(ipcRoot, "heartbeat.json");
            resultError(window.cep.fs.writeFile(heartbeatPath, JSON.stringify({ at: new Date().toISOString(), panel_id: "review_capture_panel" })), "更新录音助手心跳失败");
            writePttArm();
        }
        catch (e) { /* Worker will stop an active recording if heartbeats cease. */ }
    }
    function writePttArm() {
        if (!ipcRoot) { return; }
        var config = {
            at: new Date().toISOString(), hotkey: pttHotkey,
            enabled: !busy && !active && !!projectPath && !!selectedDevice() && workerIsOnline(),
            project_path: projectPath, session_id: sessionId, device: selectedDevice()
        };
        resultError(window.cep.fs.writeFile(join(ipcRoot, "ptt_arm.json"), JSON.stringify(config)), "更新快捷键设置失败");
    }
    function writePanelDiagnostics() {
        try {
            var selectors = ["html", "body", "header", "main", "h1", "#project-label", "#start-recording", "#notice"];
            var elements = selectors.map(function (selector) {
                var element = document.querySelector(selector);
                if (!element) { return { selector: selector, found: false }; }
                var rect = element.getBoundingClientRect();
                var style = window.getComputedStyle(element);
                return {
                    selector: selector,
                    found: true,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    color: style.color,
                    background: style.backgroundColor
                };
            });
            var styleLink = document.querySelector('link[rel="stylesheet"]');
            var info = {
                at: new Date().toISOString(),
                extension_version: "1.2.6",
                ready_state: document.readyState,
                viewport: { width: window.innerWidth, height: window.innerHeight },
                body_child_count: document.body ? document.body.children.length : -1,
                body_text_length: document.body ? (document.body.innerText || "").length : -1,
                stylesheet_count: document.styleSheets.length,
                stylesheet_loaded: !!(styleLink && styleLink.sheet),
                elements: elements
            };
            var root = ipcRoot || extensionRoot;
            var path = join(root, "panel_diagnostics.json");
            resultError(window.cep.fs.writeFile(path, JSON.stringify(info, null, 2)), "写入面板诊断记录失败");
        } catch (e) { /* Temporary startup diagnostics must never block the review panel. */ }
    }
    function beginWorkerHealth() {
        launchWorker(function (error) {
            if (error) { setWorkerStatus("助手未连接", "offline"); showNotice(error.message, "error"); return; }
            heartbeat();
            if (!heartbeatTimer) { heartbeatTimer = setInterval(heartbeat, 1000); }
            setWorkerStatus("助手已连接", "online");
            refreshDevices();
        });
        statusTimer = setInterval(updateWorkerStatus, 100);
    }
    function updateWorkerStatus() {
        if (!workerIsOnline()) { setWorkerStatus("助手未连接", "offline"); return; }
        setWorkerStatus("助手已连接", "online");
        var status = readJson(statusPath());
        var currentPtt = readJson(join(join(ipcRoot, "ptt"), "current.json"));
        if (currentPtt && currentPtt.state === "idle") { currentPtt = null; }
        handlePushToTalk(currentPtt || (status && status.push_to_talk));
        if (!status || !status.active_recording) {
            if (!active) { $("level-meter").value = 0; $("level-text").textContent = "—"; }
            return;
        }
        var recording = status.active_recording;
        if ((!active || active.noteId !== recording.note_id) && recording.note_path) {
            var note = readJson(recording.note_path);
            if (note) {
                var start = note.mode === "range" ? ((note.location || {}).endpoints || {}).start : (note.location || {}).point;
                var end = note.mode === "range" ? ((note.location || {}).endpoints || {}).end : null;
                active = {
                    noteId: note.note_id, notePath: recording.note_path, sessionId: note.session_id,
                    mode: note.mode, rangeStatus: (note.location || {}).range_status,
                    versionStatus: (note.location || {}).version_status,
                    startLabel: start ? start.time_label : "位置待核对", endLabel: end ? end.time_label : ""
                };
                sessionId = note.session_id; projectPath = note.project.path;
                $("record-state").textContent = "● 正在录音，可以继续说";
                $("record-state").className = "record-state recording";
                refreshControls();
            }
        }
        $("level-meter").value = Math.min(1, recording.input_peak || 0);
        $("level-text").textContent = recording.elapsed_seconds.toFixed(1) + " 秒";
    }
    function handlePushToTalk(capture) {
        if (!capture || !capture.id || capture.id === pttHandledId) { return; }
        if (!pttCurrent || pttCurrent.id !== capture.id) {
            pttCurrent = {
                id: capture.id, snapshot: null, snapshotPending: true, complete: false,
                releasePreviewRequested: false, releasePreview: null, releasePending: false,
                releaseReady: false, endSnapshot: null, sent: false, needsManualRetry: false
            };
            $("record-state").textContent = "已按下 " + capture.hotkey + "，正在记录并读取时间线…";
            $("record-state").className = "record-state recording";
            getSnapshot(function (error, snapshot) {
                if (!pttCurrent || pttCurrent.id !== capture.id) { return; }
                pttCurrent.snapshotPending = false;
                if (error) {
                    pttCurrent.needsManualRetry = true;
                    showNotice("声音会保留；时间线定位失败：" + error.message + "。保持播放头和选区不变，再点“重试关联”。", "error");
                    $("retry-ptt").hidden = false;
                } else {
                    pttCurrent.snapshot = snapshot;
                    setProject(snapshot);
                    if (!pttCurrent.complete) {
                        $("record-state").textContent = "● 起点已记录；可拖到终点，停稳后松开 " + capture.hotkey;
                        $("record-state").className = "record-state recording";
                    }
                    captureReleaseSnapshotIfNeeded(pttCurrent);
                }
            });
        }
        if (capture.state === "preparing" || capture.state === "recording") {
            $("record-state").textContent = pttCurrent.snapshot ?
                "● 起点已记录；可拖到终点，停稳后松开 " + capture.hotkey :
                "● 按住 " + capture.hotkey + " 说话，正在记录起点…";
            $("record-state").className = "record-state recording";
        } else if (capture.state === "complete") {
            if (!pttCurrent.complete) {
                pttCurrent.complete = true;
                $("record-state").textContent = "录音已停止，正在确认终点…";
                $("record-state").className = "record-state";
            }
            captureReleasePreview(pttCurrent);
        } else if (capture.state === "failed" || capture.state === "interrupted") {
            pttHandledId = capture.id;
            $("record-state").textContent = "快捷录音未完成";
            $("record-state").className = "record-state error";
            showNotice((capture.error || "录音中断") + "。原始采集保留在 " + capture.capture_dir, "error");
            sendRequest("ptt_ack", { capture_id: capture.id }, function (error) {
                if (error) { showNotice("录音状态未清除：" + error.message, "error"); }
            });
        }
    }
    function pttLocationError(current, message) {
        if (!pttCurrent || pttCurrent.id !== current.id) { return; }
        current.needsManualRetry = true;
        current.releasePending = false;
        $("retry-ptt").hidden = false;
        $("record-state").textContent = "声音已保存，区间位置待确认";
        $("record-state").className = "record-state error";
        showNotice(message + "。请停在终点后点“重试关联”；原录音仍保留。", "error");
    }
    function releasePreviewSnapshot(first, preview) {
        return {
            read_at: new Date().toISOString(),
            project: first.project,
            sequence: first.sequence,
            location: preview.position,
            content_fingerprint: first.content_fingerprint,
            content_fingerprint_status: first.content_fingerprint_status,
            capture_quality: "release_preview_only"
        };
    }
    function captureReleasePreview(current) {
        if (!current.complete || current.releasePreviewRequested || current.needsManualRetry) { return; }
        current.releasePreviewRequested = true;
        hostCall("getPlayheadPreview", undefined, function (error, value) {
            if (!pttCurrent || pttCurrent.id !== current.id) { return; }
            if (error || !value || !value.position || !value.position.ticks) {
                current.pointFallback = true;
                current.releaseWarning = "松开 F8 时没有读到终点；录音按起点存为单点意见，请核对范围";
                captureReleaseSnapshotIfNeeded(current);
                return;
            }
            current.releasePreview = value;
            captureReleaseSnapshotIfNeeded(current);
        });
    }
    function captureReleaseSnapshotIfNeeded(current) {
        if (!pttCurrent || pttCurrent.id !== current.id || !current.complete ||
                !current.snapshot || current.releasePending ||
                current.releaseReady || current.needsManualRetry) { return; }
        if (current.pointFallback) {
            current.releaseReady = true;
            commitPttIfReady();
            return;
        }
        if (!current.releasePreview) { return; }
        var first = current.snapshot;
        var last = current.releasePreview;
        if (String(last.project_path).toLowerCase() !== String(first.project.path).toLowerCase() ||
                String(last.sequence_id) !== String(first.sequence.host_id)) {
            pttLocationError(current, "录音期间 Premiere 工程或序列变了"); return;
        }
        if (first.selection && first.selection.length) {
            current.releaseReady = true;
            commitPttIfReady();
            return;
        }
        if (String(last.position.ticks) === String(first.location.ticks)) {
            current.releaseReady = true;
            commitPttIfReady();
            return;
        }
        current.releasePending = true;
        getSnapshot(function (error, snapshot) {
            if (!pttCurrent || pttCurrent.id !== current.id) { return; }
            current.releasePending = false;
            if (error || !snapshot || String(snapshot.location.ticks) !== String(last.position.ticks)) {
                current.endSnapshot = releasePreviewSnapshot(first, last);
                current.releaseWarning = "终点采用松开 F8 时的播放头位置；请复核区间边界";
            } else {
                current.endSnapshot = snapshot;
            }
            current.releaseReady = true;
            commitPttIfReady();
        });
    }
    function commitPttIfReady() {
        if (!pttCurrent || !pttCurrent.complete || !pttCurrent.snapshot || !pttCurrent.releaseReady ||
                pttCurrent.sent || pttCurrent.needsManualRetry) { return; }
        var current = pttCurrent;
        current.sent = true;
        sendRequest("ptt_commit", { capture_id: current.id, snapshot: current.snapshot,
            end_snapshot: current.endSnapshot }, function (error, result) {
            if (!pttCurrent || pttCurrent.id !== current.id) { return; }
            if (error) {
                current.needsManualRetry = true;
                $("retry-ptt").hidden = false;
                $("record-state").textContent = "声音已保存，位置待关联";
                $("record-state").className = "record-state error";
                showNotice(error.message + "。原录音仍在本机；可重试关联。", "error");
                return;
            }
            pttHandledId = current.id;
            pttCurrent = null;
            $("retry-ptt").hidden = true;
            $("record-state").textContent = result.mode === "range" ? "录音和区间已保存" : "录音和当前位置已保存";
            $("record-state").className = "record-state saved";
            showNotice(current.releaseWarning || ("意见已保存，可以继续按住 " + pttHotkey + " 录下一条。"), current.releaseWarning ? "error" : "good");
            refreshNotes(); refreshControls();
        }, 25000);
    }
    function retryPtt() {
        if (!pttCurrent || !pttCurrent.complete) { return; }
        $("retry-ptt").hidden = true;
        pttCurrent.sent = false;
        pttCurrent.needsManualRetry = false;
        pttCurrent.releasePreviewRequested = false;
        pttCurrent.releasePreview = null;
        pttCurrent.releaseReady = false;
        pttCurrent.endSnapshot = null;
        pttCurrent.pointFallback = false;
        pttCurrent.releaseWarning = "";
        if (pttCurrent.snapshot) {
            captureReleasePreview(pttCurrent); return;
        }
        getSnapshot(function (error, snapshot) {
            if (!pttCurrent) { return; }
            if (error) { showNotice(error.message, "error"); $("retry-ptt").hidden = false; return; }
            pttCurrent.snapshot = snapshot;
            captureReleasePreview(pttCurrent);
        });
    }
    function refreshDevices() {
        sendRequest("devices", {}, function (error, result) {
            var select = $("device-select");
            select.innerHTML = "";
            if (error) {
                select.disabled = true;
                var fail = document.createElement("option"); fail.textContent = "无法读取麦克风"; select.appendChild(fail);
                showNotice(error.message, "error"); return;
            }
            devices = result.devices || [];
            if (!devices.length) {
                select.disabled = true;
                var none = document.createElement("option"); none.textContent = "没有可用的输入设备"; select.appendChild(none);
                refreshControls(); return;
            }
            showAllDevices = false;
            renderDeviceOptions();
            select.disabled = false;
            refreshControls();
            if (projectPath) { refreshNotes(); }
        });
    }
    function renderDeviceOptions() {
        var select = $("device-select");
        select.innerHTML = "";
        var preferred = [];
        devices.forEach(function (device, i) {
            if (device.default || (device.hostapi === "Windows WASAPI" &&
                !/loopback|oculus|virtual/i.test(device.name))) { preferred.push(i); }
        });
        var indexes = showAllDevices ? devices.map(function (_, i) { return i; }) : preferred;
        indexes.forEach(function (i) {
            var device = devices[i];
                var option = document.createElement("option");
                option.value = String(i);
                option.textContent = (device.default ? "推荐 · 系统默认：" : "其他输入：") + device.name +
                    (showAllDevices ? " · " + device.hostapi : "");
                select.appendChild(option);
        });
        if (!showAllDevices && preferred.length < devices.length) {
            var more = document.createElement("option");
            more.value = "more"; more.textContent = "显示全部设备（高级）…";
            select.appendChild(more);
        }
        var defaultIndex = devices.findIndex(function (device) { return device.default; });
        select.value = String(defaultIndex >= 0 ? defaultIndex : indexes[0]);
    }
    function selectedDevice() {
        var select = $("device-select");
        return devices[Number(select.value)] || null;
    }
    function getSnapshot(callback) {
        hostCall("captureStableSnapshot", undefined, callback);
    }
    function getPreview() {
        hostCall("getPlayheadPreview", undefined, function (error, value) {
            if (error) { $("playhead-label").textContent = "无法读取播放头"; return; }
            if (projectPath && value.project_path &&
                    value.project_path.toLowerCase() !== projectPath.toLowerCase()) {
                if (active || pttCurrent || busy) {
                    showNotice("Premiere 工程已切换；当前录音结束后请重新读取工程。", "error");
                    return;
                }
                if (!contextRefreshing) { contextRefreshing = true; initialContext(); }
                return;
            }
            $("playhead-label").textContent = value.position.time_label || ("ticks " + value.position.ticks);
            $("sequence-label").textContent = value.sequence_name || "当前序列";
        });
    }
    function sessionFor(path) {
        if (!path) { return ""; }
        var key = "autovideoedit.review.session." + path.toLowerCase();
        var value = window.localStorage.getItem(key);
        if (!value) { value = guid(); window.localStorage.setItem(key, value); }
        return value;
    }
    function setProject(snapshot) {
        if (projectPath && projectPath !== snapshot.project.path) { currentNotes = []; }
        projectPath = snapshot.project.path;
        sessionId = sessionFor(projectPath);
        $("project-label").textContent = snapshot.project.name + " · " + snapshot.sequence.name;
        $("sequence-label").textContent = snapshot.sequence.name;
        $("playhead-label").textContent = snapshot.location.time_label || ("ticks " + snapshot.location.ticks);
        refreshControls();
    }
    function initialContext() {
        getSnapshot(function (error, snapshot) {
            contextRefreshing = false;
            if (error) {
                $("project-label").textContent = error.message;
                setWorkerStatus("等待 Premiere", "offline");
                refreshControls(); return;
            }
            setProject(snapshot);
            refreshNotes();
        });
    }
    function refreshControls() {
        var recording = !!active;
        var sameSessionTranscribing = !!transcriptionActive && transcriptionActive.sessionId === sessionId;
        var queuedForSession = transcriptionQueue.some(function (item) { return item.sessionId === sessionId; });
        var canRecord = !busy && !recording && !!projectPath && !!selectedDevice() && workerIsOnline();
        $("start-recording").disabled = !canRecord;
        $("record-controls").hidden = !recording && selectedMode() !== "range";
        document.querySelectorAll('input[name="mode"]').forEach(function (el) { el.disabled = recording || busy; });
        $("device-select").disabled = recording || busy || !devices.length;
        $("refresh-devices").disabled = recording || busy;
        $("new-session").disabled = recording || busy || !projectPath;
        $("package-notes").disabled = recording || !!pttCurrent || busy || sameSessionTranscribing || queuedForSession || !projectPath || !currentNotes.length;
        $("revalidate-batch").disabled = recording || busy || !projectPath || !sessionId || !workerIsOnline();
        $("lock-end").disabled = busy || !recording || !!(active && (active.locking || active.stopping));
        $("reselect-end").disabled = busy || !recording || !!(active && (active.locking || active.stopping));
        $("stop-recording").disabled = busy || !recording || !!(active && active.stopping);
        $("cancel-recording").disabled = busy || !recording || !!(active && active.stopping);
        $("start-recording").textContent = selectedMode() === "range" ? "● 开始区间意见" : "● 开始录音";
        $("start-recording").hidden = recording || selectedMode() !== "range";
        $("mode-hint").hidden = recording || selectedMode() !== "range";
        $("lock-end").hidden = !(recording && active.mode === "range" && active.rangeStatus === "awaiting_end");
        $("reselect-end").hidden = !(recording && active.mode === "range" && active.rangeStatus === "locked");
        $("stop-recording").hidden = !recording;
        $("cancel-recording").hidden = !recording;
        $("range-state").hidden = !(recording && active.mode === "range");
        if (recording && active.mode === "range") {
            $("range-state").textContent = active.rangeStatus === "locked" ?
                "起点 " + active.startLabel + " → 终点 " + active.endLabel + " · 已锁定；继续移动播放头不会改范围。" +
                    (active.versionStatus === "partial" ? " 时间线核验覆盖不全，后续剪辑前需人工核对。" : "") :
                "起点 " + active.startLabel + " → 终点待确认；拖动播放头，停稳后锁定终点或直接结束。";
        }
        if (!recording) {
            $("mode-hint").textContent = selectedMode() === "range" ?
                "先停在范围起点。录音后可拖动播放头；结束时只记录起点和确认的终点。" :
                "暂停并停在问题处。录音开始后，这个位置会固定。";
        }
    }
    function selectedMode() {
        var selected = document.querySelector('input[name="mode"]:checked');
        return selected ? selected.value : "point";
    }
    function startRecording() {
        if (busy || active) { return; }
        busy = true; refreshControls(); $("record-state").textContent = "正在读取位置并启动麦克风…"; $("record-state").className = "record-state";
        getSnapshot(function (error, snapshot) {
            if (error) { busy = false; $("record-state").textContent = "准备失败"; $("record-state").className = "record-state error"; showNotice(error.message, "error"); refreshControls(); return; }
            setProject(snapshot);
            var mode = selectedMode();
            var operationId = guid();
            var clickedAt = new Date().toISOString();
            sendRequest("start", { mode: mode, snapshot: snapshot, session_id: sessionId, device: selectedDevice(), operation_id: operationId, clicked_at: clickedAt }, function (startError, result) {
                busy = false;
                if (startError) {
                    $("record-state").textContent = "录音没有启动"; $("record-state").className = "record-state error";
                    showNotice(startError.message, "error"); refreshNotes(); refreshControls(); return;
                }
                active = {
                    noteId: result.note_id, notePath: result.note_path, sessionId: result.session_id,
                    mode: mode, rangeStatus: result.location.range_status,
                    startLabel: mode === "range" ? result.location.endpoints.start.time_label : result.location.point.time_label,
                    endLabel: "", stopOperationId: null
                };
                if (mode === "point") { active.startLabel = result.location.point.time_label; }
                $("record-state").textContent = "● 录音中，可以说了"; $("record-state").className = "record-state recording";
                showNotice("位置和录音已绑定到同一条意见。", "good");
                refreshControls();
            }, 15000);
        });
    }
    function captureAnd(action, payload, callback, shouldContinue) {
        getSnapshot(function (error, snapshot) {
            if (shouldContinue && !shouldContinue()) { return; }
            if (error) { callback(error); return; }
            payload.snapshot = snapshot;
            sendRequest(action, payload, callback, 15000);
        });
    }
    function lockEnd() {
        if (!active || active.mode !== "range" || busy) { return; }
        var noteId = active.noteId;
        active.locking = true; refreshControls(); $("record-state").textContent = "正在锁定终点；录音继续…";
        var operationId = guid();
        captureAnd("lock_end", { note_id: active.noteId, operation_id: operationId, clicked_at: new Date().toISOString() }, function (error, result) {
            if (!active || active.noteId !== noteId || active.stopping) { return; }
            active.locking = false;
            if (error) { $("record-state").textContent = "终点没有锁定，录音仍在继续"; $("record-state").className = "record-state error"; showNotice(error.message, "error"); }
            else {
                active.rangeStatus = result.location.range_status;
                if (active.rangeStatus === "locked") {
                    active.endLabel = result.location.endpoints.end.time_label;
                    active.versionStatus = result.location.version_status;
                    $("record-state").textContent = "● 录音中 · 终点已锁定"; $("record-state").className = "record-state recording";
                    showNotice(active.versionStatus === "partial" ?
                        "终点已固定；时间线版本检查覆盖不全，剪辑前请人工核对。" :
                        "终点已固定；之后移动播放头不会改变范围。", active.versionStatus === "partial" ? "error" : "good");
                } else {
                    $("record-state").textContent = "位置需核对，录音仍在继续"; $("record-state").className = "record-state error";
                    showNotice("Premiere 检查未通过：" + (result.location.stale_reason || "范围无效"), "error");
                }
            }
            refreshControls();
        }, function () { return !!active && active.noteId === noteId && !active.stopping; });
    }
    function reselectEnd() {
        if (!active || active.mode !== "range" || busy) { return; }
        busy = true; refreshControls();
        sendRequest("reselect_end", { note_id: active.noteId, operation_id: guid(), clicked_at: new Date().toISOString() }, function (error, result) {
            busy = false;
            if (error) { showNotice(error.message, "error"); }
            else { active.rangeStatus = result.location.range_status; active.endLabel = ""; $("record-state").textContent = "● 录音中 · 请重新选择终点"; showNotice("旧终点已留档；移动播放头后再锁定新终点。", "good"); }
            refreshControls();
        });
    }
    function stopRecording() {
        if (!active || busy) { return; }
        busy = true; refreshControls(); $("record-state").textContent = "正在停止录音并保存…"; $("record-state").className = "record-state";
        var note = active;
        active.stopping = true;
        var directRangeEnd = note.mode === "range" && note.rangeStatus === "awaiting_end";
        var stopOperationId = guid();
        var clickedAt = new Date().toISOString();
        var snapshotResult = null, snapshotError = null, stopResult = null, stopError = null, done = false;
        function finishIfReady() {
            if (done || stopResult === null && stopError === null || directRangeEnd && snapshotResult === null && snapshotError === null) { return; }
            done = true;
            if (stopError) {
                busy = false; $("record-state").textContent = "保存结果待确认"; $("record-state").className = "record-state error";
                showNotice(stopError.message + "。不要重复点击；稍后刷新本批意见。", "error"); refreshControls(); return;
            }
            active = null;
            if (directRangeEnd && snapshotResult) {
                sendRequest("attach_endpoint", { note_id: note.noteId, note_path: stopResult.note_path || note.notePath, operation_id: stopOperationId, clicked_at: clickedAt, snapshot: snapshotResult }, function (anchorError, anchored) {
                    busy = false;
                    if (anchorError) {
                        $("record-state").textContent = "录音已保存，范围待补充"; $("record-state").className = "record-state error";
                        showNotice("声音已保存；终点没有确认，请稍后在本批意见里补充定位。（" + anchorError.message + "）", "error");
                    } else if (anchored.location.range_status !== "locked") {
                        $("record-state").textContent = "录音已保存，位置需核对"; $("record-state").className = "record-state error";
                        showNotice("声音已保存，但区间没有锁定：" + (anchored.location.stale_reason || "请检查范围"), "error");
                    } else {
                        $("record-state").textContent = "录音和区间已保存"; $("record-state").className = "record-state saved";
                        showNotice(anchored.location.version_status === "partial" ?
                            "起点、终点和意见录音都已保存；时间线版本检查覆盖不全，后续剪辑前需人工核对。" :
                            "起点、终点和意见录音都已保存。", anchored.location.version_status === "partial" ? "error" : "good");
                    }
                    refreshNotes(); refreshControls();
                }, 15000);
                return;
            }
            busy = false;
            if (stopResult.status.recording === "saved") {
                $("record-state").textContent = stopResult.location.range_status === "needs_endpoint" ? "录音已保存，范围待补充" : "录音已保存";
                $("record-state").className = stopResult.location.range_status === "needs_endpoint" ? "record-state error" : "record-state saved";
                showNotice(stopResult.location.range_status === "needs_endpoint" ? "声音已保存；终点未锁定，不能把它当作完整区间。" : "意见录音已完整保存。", stopResult.location.range_status === "needs_endpoint" ? "error" : "good");
            } else {
                $("record-state").textContent = "录音已中断；已采到的内容已保留"; $("record-state").className = "record-state error";
                showNotice(stopResult.status.error || "请回听已保存部分。", "error");
            }
            refreshNotes(); refreshControls();
        }
        if (directRangeEnd) {
            getSnapshot(function (error, snapshot) { snapshotError = error; snapshotResult = snapshot || false; finishIfReady(); });
        }
        sendRequest("stop", { note_id: note.noteId, operation_id: stopOperationId, clicked_at: clickedAt }, function (error, result, late) {
            if (late && result) { done = false; stopError = null; busy = true; }
            stopError = error; stopResult = result || false; finishIfReady();
        }, 25000);
    }
    function cancelRecording() {
        if (!active || busy) { return; }
        active.stopping = true;
        busy = true; refreshControls();
        sendRequest("cancel", { note_id: active.noteId }, function (error, result) {
            busy = false; active = null;
            if (error) { $("record-state").textContent = "取消结果待确认"; showNotice(error.message, "error"); }
            else { $("record-state").textContent = result.status.recording === "cancelled" ? "已取消（没有有效录音）" : "已停止；已有声音保留为未处理记录"; showNotice("原始录音没有删除。", "good"); }
            refreshNotes(); refreshControls();
        }, 25000);
    }
    function refreshNotes() {
        if (!projectPath || !sessionId || !workerIsOnline()) { return; }
        var requestedSessionId = sessionId;
        sendRequest("list_notes", { project_path: projectPath, session_id: requestedSessionId }, function (error, result) {
            if (error) { showNotice(error.message, "error"); return; }
            if (requestedSessionId !== sessionId) { return; }
            currentNotes = result.notes || [];
            renderNotes(currentNotes);
            queuePendingTranscriptions(currentNotes);
            refreshControls();
        });
    }
    function fmtLocation(note) {
        var location = note.location || {};
        if (note.mode === "range") {
            var endpoints = location.endpoints || {};
            if (endpoints.start && endpoints.end) { return endpoints.start.time_label + " → " + endpoints.end.time_label + " · " + location.range_status; }
            return "区间待补充 · " + (location.range_status || "unknown");
        }
        return location.point ? location.point.time_label : "位置待确认";
    }
    function noteDir(note) {
        var normalized = projectPath.replace(/\//g, "\\");
        var projectRoot = normalized.substring(0, normalized.lastIndexOf("\\"));
        return join(join(join(join(projectRoot, "work\\premiere-review"), note.session_id), "notes"), note.note_id);
    }
    function projectRootPath(path) {
        var normalized = String(path || projectPath).replace(/\//g, "\\");
        return normalized.substring(0, normalized.lastIndexOf("\\"));
    }
    function fileUrl(path) { return "file:///" + encodeURI(String(path).replace(/\\/g, "/")).replace(/#/g, "%23"); }
    function renderNotes(notes) {
        var list = $("notes-list"); list.innerHTML = "";
        if (!notes.length) { var empty = document.createElement("div"); empty.className = "empty"; empty.textContent = "还没有意见"; list.appendChild(empty); return; }
        notes.forEach(function (note, index) {
            var card = document.createElement("article"); card.className = "note";
            var head = document.createElement("div"); head.className = "note-head";
            var title = document.createElement("span"); title.textContent = (index + 1 < 10 ? "0" : "") + (index + 1) + " · " + (note.mode === "range" ? "区间意见" : "单点意见");
            var transcript = note.transcript || {};
            var key = note.session_id + ":" + note.note_id;
            var job = readJson(join(noteDir(note), "transcription\\job.json"));
            var waiting = (transcriptionActive && transcriptionActive.key === key) ||
                transcriptionQueue.some(function (item) { return item.key === key; });
            var transcriptState = transcript.edited_text ? "已修订" : transcript.state === "transcribed" ?
                (transcript.text ? "已转写" : "未识别到文字") :
                ((job && job.status === "failed") || transcriptionFailed[key] ? "转写失败" : waiting ? "转写中" : "待自动转写");
            var status = document.createElement("span"); status.className = "note-meta";
            status.textContent = note.status.recording + " · " + transcriptState + (note.status.editing === "cancelled" ? " · 已取消" : "");
            head.appendChild(title); head.appendChild(status); card.appendChild(head);
            var meta = document.createElement("div"); meta.className = "note-meta"; meta.textContent = fmtLocation(note); card.appendChild(meta);
            if (note.mode === "range" && note.location && (note.location.version_status === "partial" || note.location.automatic_edit_allowed === false)) {
                var warning = document.createElement("div"); warning.className = "note-meta warning";
                warning.textContent = "时间线版本核验覆盖不全；用于剪辑前请人工回看确认。"; card.appendChild(warning);
            }
            var text = document.createElement("textarea");
            text.className = "note-transcript";
            text.rows = 3;
            var savedText = transcript.edited_text || transcript.text || "";
            text.value = Object.prototype.hasOwnProperty.call(transcriptDrafts, key) ? transcriptDrafts[key] : savedText;
            text.placeholder = "听回原声后，可在这里补充或修订转写文字。";
            text.setAttribute("aria-label", "意见转写文字");
            text.addEventListener("input", function () {
                if (text.value === savedText) { delete transcriptDrafts[key]; }
                else { transcriptDrafts[key] = text.value; }
            });
            card.appendChild(text);
            var actions = document.createElement("div"); actions.className = "note-actions";
            var canEditText = note.status && (note.status.recording === "saved" || note.status.recording === "interrupted");
            var saveText = document.createElement("button"); saveText.textContent = "保存文字修订"; saveText.disabled = !canEditText;
            saveText.addEventListener("click", function () {
                saveText.disabled = true;
                showNotice("正在保存这条意见的文字修订…");
                sendRequest("correct_transcript", {
                    project_path: projectPath,
                    session_id: note.session_id,
                    note_id: note.note_id,
                    expected_transcript_revision: transcript.revision === undefined ? null : transcript.revision,
                    text: text.value,
                    editor: "Premiere 审片面板"
                }, function (error, result) {
                    if (error) { saveText.disabled = false; showNotice(error.message, "error"); return; }
                    delete transcriptDrafts[key];
                    showNotice("这条意见的文字已保存，发送本批意见时会自动带上。", "good");
                    refreshNotes();
                });
            });
            actions.appendChild(saveText);
            if ((job && job.status === "failed") || transcriptionFailed[key]) {
                var retry = document.createElement("button"); retry.textContent = "重试转写";
                retry.addEventListener("click", function () {
                    delete transcriptionFailed[key];
                    enqueueTranscription(note, true);
                });
                actions.appendChild(retry);
            }
            var anchor = note.mode === "range" ? (note.location.endpoints || {}).start : (note.location || {}).point;
            if (anchor && anchor.ticks) {
                var jump = document.createElement("button"); jump.textContent = "回到起点";
                jump.addEventListener("click", function () { hostCall("seekToTicks", anchor.ticks, function (error) { showNotice(error ? error.message : "播放头已移到意见起点。", error ? "error" : "good"); }); });
                actions.appendChild(jump);
            }
            card.appendChild(actions);
            var audioPath = noteDir(note) + "\\audio.wav";
            var exists = null;
            try { exists = window.cep.fs.stat(audioPath); } catch (e) { exists = null; }
            if (exists && exists.err === 0) {
                var audio = document.createElement("audio"); audio.controls = true; audio.preload = "none"; audio.src = fileUrl(audioPath); card.appendChild(audio);
            }
            list.appendChild(card);
        });
    }
    function transcriptionKey(note) { return note.session_id + ":" + note.note_id; }
    function transcriptionJobPath(item) {
        return join(join(join(join(projectRootPath(item.projectPath), "work\\premiere-review"), item.sessionId), "notes"), item.noteId + "\\transcription\\job.json");
    }
    function queuePendingTranscriptions(notes) {
        notes.forEach(function (note) {
            var transcript = note.transcript || {};
            if (note.status.recording !== "saved" && note.status.recording !== "interrupted") { return; }
            if (transcript.edited_text || transcript.state === "transcribed") { return; }
            var key = transcriptionKey(note);
            var job = readJson(join(noteDir(note), "transcription\\job.json"));
            if (transcriptionFailed[key] || (job && job.status === "failed")) { return; }
            if (job && job.status === "completed") { return; }
            enqueueTranscription(note, false);
        });
    }
    function enqueueTranscription(note, force) {
        var key = transcriptionKey(note);
        if (transcriptionActive && transcriptionActive.key === key) { return; }
        if (transcriptionQueue.some(function (item) { return item.key === key; })) { return; }
        var item = { key: key, sessionId: note.session_id, noteId: note.note_id, projectPath: (note.project || {}).path || projectPath, force: !!force };
        transcriptionQueue.push(item);
        refreshControls();
        startNextTranscription();
    }
    function finishTranscription(item, error) {
        if (transcriptionTimer) { clearInterval(transcriptionTimer); transcriptionTimer = null; }
        if (error) {
            transcriptionFailed[item.key] = true;
            showNotice("一条意见转写失败，录音仍在。可点该条的“重试转写”：" + error, "error");
        }
        transcriptionActive = null;
        if (sessionId === item.sessionId) { refreshNotes(); }
        refreshControls();
        startNextTranscription();
    }
    function pollNoteTranscription(item, acceptedJobIds) {
        if (transcriptionTimer) { clearInterval(transcriptionTimer); }
        transcriptionStartedAt = Date.now();
        transcriptionJobSeen = false;
        transcriptionTimer = setInterval(function () {
            var job = readJson(transcriptionJobPath(item));
            var expected = job && job.session_id === item.sessionId && job.note_id === item.noteId &&
                acceptedJobIds.indexOf(job.job_id) !== -1;
            if (expected) { transcriptionJobSeen = true; }
            if (expected && (job.status === "completed" || job.status === "failed")) {
                finishTranscription(item, job.status === "failed" ? (job.error || "请检查本条转写状态") : null);
            } else if (!transcriptionJobSeen && Date.now() - transcriptionStartedAt > 15000) {
                finishTranscription(item, "后台进程没有返回状态");
            } else if (Date.now() - transcriptionStartedAt > 30 * 60 * 1000) {
                finishTranscription(item, "转写超时");
            }
        }, 1500);
    }
    function startNextTranscription() {
        if (transcriptionActive || !transcriptionQueue.length) { return; }
        var item = transcriptionQueue.shift();
        transcriptionActive = item;
        refreshControls();
        var existing = readJson(transcriptionJobPath(item));
        if (!runtimeConfig || !runtimeConfig.transcription_python || !runtimeConfig.note_transcription_entry ||
                !window.cep.process || typeof window.cep.process.createProcess !== "function") {
            finishTranscription(item, "本机转写程序没有配置好");
            return;
        }
        var jobId = guid();
        var args = [String(runtimeConfig.transcription_python), String(runtimeConfig.note_transcription_entry),
            "--project-root", projectRootPath(item.projectPath), "--session-id", item.sessionId,
            "--note-id", item.noteId, "--job-id", jobId, "--model", runtimeConfig.transcription_model || "medium"];
        var launch = window.cep.process.createProcess.apply(window.cep.process, args);
        if (!launch || launch.err !== 0) {
            finishTranscription(item, "后台转写没有启动（错误码 " + (launch ? launch.err : "未知") + "）");
            return;
        }
        var accepted = [jobId];
        if (existing && existing.status === "running") { accepted.push(existing.job_id); }
        pollNoteTranscription(item, accepted);
    }
    function packageNotes() {
        if (busy || active || pttCurrent ||
                (transcriptionActive && transcriptionActive.sessionId === sessionId) ||
                transcriptionQueue.some(function (item) { return item.sessionId === sessionId; })) { return; }
        if (Object.keys(transcriptDrafts).some(function (key) { return key.indexOf(sessionId + ":") === 0; })) {
            showNotice("还有文字改动未保存；请先点对应意见的“保存文字修订”。", "error");
            return;
        }
        var targetSessionId = sessionId;
        busy = true; refreshControls(); showNotice("正在核对并发送本批意见…");
        sendRequest("list_notes", { project_path: projectPath, session_id: targetSessionId }, function (listError, listed) {
            if (listError) { busy = false; refreshControls(); showNotice(listError.message, "error"); return; }
            var pending = (listed.notes || []).filter(function (note) {
                var transcript = note.transcript || {};
                return note.status.recording === "saved" && !transcript.edited_text &&
                    (transcript.state !== "transcribed" || !transcript.text);
            });
            if (pending.length) {
                busy = false; refreshControls();
                showNotice("还有 " + pending.length + " 条意见没有转写文字；可在各条下重试转写。", "error");
                return;
            }
            sendRequest("package", { project_path: projectPath, session_id: targetSessionId }, function (error, result) {
                busy = false; refreshControls();
                if (error) { showNotice(error.message, "error"); }
                else if (result.decision_intake_error) { showNotice("意见已保存，但预解析失败：" + result.decision_intake_error, "error"); }
                else { showNotice("本批意见已保存，文字动作清单：" + result.decision_intake + "。下一步核对当前时间线并生成剪辑 dry-run。", "good"); }
            }, 25000);
        }, 25000);
    }
    function revalidateBatch() {
        if (busy || active || !projectPath || !sessionId) { return; }
        busy = true; refreshControls(); showNotice("正在读取当前 Premiere 时间线并核对本批意见…");
        getSnapshot(function (snapshotError, snapshot) {
            if (snapshotError) {
                busy = false; refreshControls(); showNotice(snapshotError.message, "error"); return;
            }
            var currentProject = String((snapshot.project || {}).path || "");
            if (!currentProject || currentProject.toLowerCase() !== projectPath.toLowerCase()) {
                busy = false; refreshControls(); showNotice("Premiere 当前工程已变化；请刷新面板上下文后再核对。", "error"); return;
            }
            sendRequest("revalidate_batch", {
                project_path: projectPath,
                session_id: sessionId,
                snapshot: snapshot
            }, function (error, result) {
                busy = false; refreshControls();
                if (error) { showNotice(error.message, "error"); return; }
                var summary = result.summary || {};
                showNotice(
                    "时间线核对报告已保存：" + (summary.matched || 0) + " 条一致，" +
                    (summary.manual_review_required || 0) + " 条需人工核对，" +
                    (summary.changed_or_incomplete || 0) + " 条不一致或定位不完整。不会自动执行剪辑。报告：" + result.report_path,
                    summary.changed_or_incomplete ? "error" : "good"
                );
            }, 25000);
        });
    }
    function startNewSession() {
        if (busy || active || pttCurrent || !projectPath) { return; }
        var key = "autovideoedit.review.session." + projectPath.toLowerCase();
        sessionId = guid(); window.localStorage.setItem(key, sessionId);
        currentNotes = [];
        renderNotes([]); $("record-state").textContent = "新的一批意见"; showNotice("新批次已创建；旧批次仍保留。", "good"); refreshControls();
    }
    function init() {
        cs = new CSInterface();
        extensionRoot = cs.getSystemPath(SystemPath.EXTENSION);
        $("start-recording").addEventListener("click", startRecording);
        $("retry-ptt").addEventListener("click", retryPtt);
        pttHotkey = window.localStorage.getItem("autovideoedit.review.ptt.hotkey") === "F9" ? "F9" : "F8";
        $("ptt-key").value = pttHotkey; $("ptt-key-label").textContent = pttHotkey;
        $("ptt-key").addEventListener("change", function () {
            pttHotkey = $("ptt-key").value === "F9" ? "F9" : "F8";
            $("ptt-key-label").textContent = pttHotkey;
            window.localStorage.setItem("autovideoedit.review.ptt.hotkey", pttHotkey);
            try { writePttArm(); } catch (e) { showNotice(e.message, "error"); }
        });
        $("lock-end").addEventListener("click", lockEnd);
        $("reselect-end").addEventListener("click", reselectEnd);
        $("stop-recording").addEventListener("click", stopRecording);
        $("cancel-recording").addEventListener("click", cancelRecording);
        $("refresh-devices").addEventListener("click", refreshDevices);
        $("device-select").addEventListener("change", function () {
            if ($("device-select").value === "more") { showAllDevices = true; renderDeviceOptions(); }
            try { writePttArm(); } catch (e) { showNotice(e.message, "error"); }
            refreshControls();
        });
        $("package-notes").addEventListener("click", packageNotes);
        $("revalidate-batch").addEventListener("click", revalidateBatch);
        $("new-session").addEventListener("click", startNewSession);
        document.querySelectorAll('input[name="mode"]').forEach(function (el) { el.addEventListener("change", refreshControls); });
        beginWorkerHealth();
        setTimeout(probeHostBridge, 1200);
        setTimeout(writePanelDiagnostics, 1500);
        initialContext();
        previewTimer = setInterval(getPreview, 500);
        window.addEventListener("beforeunload", function () {
            if (heartbeatTimer) { clearInterval(heartbeatTimer); }
            if (previewTimer) { clearInterval(previewTimer); }
            if (statusTimer) { clearInterval(statusTimer); }
            if (transcriptionTimer) { clearInterval(transcriptionTimer); }
        });
        $("record-state").textContent = "准备就绪";
    }
    if (typeof module !== "undefined" && module.exports) {
        module.exports = { releasePreviewSnapshot: releasePreviewSnapshot };
    }
    if (typeof document !== "undefined") { document.addEventListener("DOMContentLoaded", init); }
}());
