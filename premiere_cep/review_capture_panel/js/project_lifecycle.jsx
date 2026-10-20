// Background project round trips. ES3-compatible; no QE/private API calls.
// A rollback opens a checkpoint copy; it never overwrites the source project.
var AVE_BRIDGE_VERSION = "1.1.0";

// Claim before touching the project. Closing/opening a project can reload CEP:
// the request must already be invisible to a fresh panel, and the host must
// persist its response without relying on the old panel's callback surviving.
function dispatchBridgeRequest(requestId, command, paramsJson, bridgeDir) {
    if (!/^[A-Za-z0-9_-]+$/.test(requestId || "")) return jsonResponse(false, null, "Invalid request ID");
    var request = new File(bridgeDir + "/commands/" + requestId + ".json");
    var running = new File(bridgeDir + "/commands/" + requestId + ".running");
    var response = new File(bridgeDir + "/responses/" + requestId + ".json");
    if (response.exists) return JSON.stringify(ave_read(response));
    if (!request.exists || running.exists) return jsonResponse(false, null, "OUTCOME_UNKNOWN: request already claimed");
    if (!request.rename(requestId + ".running")) return jsonResponse(false, null, "Cannot claim request");
    var result;
    try {
        var envelope = ave_read(running);
        if (envelope.request_id !== requestId || envelope.command !== command || JSON.stringify(envelope.params || {}) !== paramsJson) {
            throw new Error("Request envelope mismatch");
        }
        if (envelope.expires_at && Date.now() > envelope.expires_at * 1000) throw new Error("Request expired before execution");
        result = JSON.parse(routeCommand(command, paramsJson));
    } catch (e) { result = {success: false, data: null, error: e.toString()}; }
    result.request_id = requestId; result.timestamp = Date.now();
    var temporary = new File(bridgeDir + "/responses/" + requestId + ".tmp");
    ave_writeNew(temporary, result);
    if (!temporary.rename(requestId + ".json")) throw new Error("Cannot publish host response");
    running.remove();
    return JSON.stringify(result);
}

function ave_path(path) {
    return new File(path).fsName.replace(/\\/g, "/").toLowerCase();
}
function ave_project(params) {
    var p = ensureProject();
    if (!params.expected_project_path || ave_path(p.path) !== ave_path(params.expected_project_path)) {
        throw new Error("PROJECT_MISMATCH: expected_project_path must match the active project");
    }
    return p;
}
function ave_output(path, extension) {
    if (!path || !new RegExp("\\." + extension + "$", "i").test(path)) {
        throw new Error("Invalid output path; expected ." + extension);
    }
    var f = new File(path);
    if (!f.parent.exists) throw new Error("Output parent does not exist: " + f.parent.fsName);
    if (f.exists) throw new Error("OUTPUT_EXISTS: " + f.fsName);
    return f;
}
function ave_writeNew(file, value) {
    if (file.exists) throw new Error("OUTPUT_EXISTS: " + file.fsName);
    file.encoding = "UTF-8";
    if (!file.open("w")) throw new Error("Cannot write: " + file.fsName);
    try { file.write(JSON.stringify(value)); } finally { file.close(); }
}
function ave_read(file) {
    file.encoding = "UTF-8";
    if (!file.open("r")) throw new Error("Cannot read: " + file.fsName);
    try { return JSON.parse(file.read().replace(/^\uFEFF/, "")); } finally { file.close(); }
}
function ave_sequences(project) {
    var out = [];
    for (var i = 0; i < project.sequences.numSequences; i++) {
        var s = project.sequences[i];
        out.push({ id: s.sequenceID, name: s.name });
    }
    return out;
}
function ave_findSequence(project, params) {
    var found = [];
    for (var i = 0; i < project.sequences.numSequences; i++) {
        var s = project.sequences[i];
        if (params.sequence_id ? s.sequenceID === params.sequence_id : s.name === params.sequence_name) found.push(s);
    }
    if (found.length !== 1) throw new Error("SEQUENCE_NOT_UNIQUE: select one sequence by ID (matches=" + found.length + ")");
    return found[0];
}
function ave_state(project) {
    var s = project.activeSequence;
    return { project_path: project.path, sequence_count: project.sequences.numSequences,
        active_sequence_id: s ? s.sequenceID : null, active_sequence_name: s ? s.name : null };
}
function ave_result(action) {
    try { return jsonResponse(true, action()); }
    catch (e) { return jsonResponse(false, null, e.toString()); }
}

function save_project(paramsJson) {
    return ave_result(function () {
        var params = JSON.parse(paramsJson || "{}");
        var project = ave_project(params);
        if (params.output_path && ave_path(params.output_path) !== ave_path(project.path)) {
            var target = ave_output(params.output_path, "prproj");
            var rc = project.saveAs(target.fsName);
            if (rc !== 0 && rc !== true) throw new Error("saveAs failed: " + rc);
            project = ensureProject();
            if (ave_path(project.path) !== ave_path(target.fsName)) throw new Error("saveAs path verification failed");
        } else {
            if (new File(project.path + ".ave-checkpoint.json").exists) {
                throw new Error("READ_ONLY_CHECKPOINT: save to a new project path first");
            }
            var saved = project.save();
            if (saved !== 0 && saved !== true) throw new Error("save failed: " + saved);
        }
        var file = new File(project.path);
        if (!file.exists || file.length === 0) throw new Error("Saved project missing or empty");
        var data = ave_state(project); data.saved = true; data.file_size = file.length;
        return data;
    });
}

function create_checkpoint(paramsJson) {
    return ave_result(function () {
        var params = JSON.parse(paramsJson || "{}");
        var project = ave_project(params);
        var target = ave_output(params.checkpoint_path, "prproj");
        var manifest = new File(target.fsName + ".ave-checkpoint.json");
        if (manifest.exists) throw new Error("Checkpoint manifest already exists");
        if (new File(project.path + ".ave-checkpoint.json").exists) throw new Error("Save a working copy before checkpointing");
        var saved = project.save();
        if (saved !== 0 && saved !== true) throw new Error("Cannot save before checkpoint: " + saved);
        if (!new File(project.path).copy(target.fsName)) throw new Error("Checkpoint copy failed");
        var data = ave_state(project);
        data.schema_version = "1.0"; data.checkpoint_path = target.fsName;
        data.sequences = ave_sequences(project); data.created_at = Date.now();
        data.file_size = new File(target.fsName).length;
        ave_writeNew(manifest, data);
        return data;
    });
}

function activate_sequence(paramsJson) {
    return ave_result(function () {
        var params = JSON.parse(paramsJson || "{}");
        var project = ave_project(params);
        var s = ave_findSequence(project, params);
        if (params.position_ticks !== undefined && !/^\d+$/.test(String(params.position_ticks))) {
            throw new Error("position_ticks must be a nonnegative integer string");
        }
        if (!project.openSequence(s.sequenceID)) throw new Error("openSequence failed");
        if (params.position_ticks !== undefined) {
            s.setPlayerPosition(String(params.position_ticks));
        }
        if (!project.activeSequence || project.activeSequence.sequenceID !== s.sequenceID) throw new Error("Active sequence verification failed");
        return ave_state(project);
    });
}

function import_timeline(paramsJson) {
    return ave_result(function () {
        var params = JSON.parse(paramsJson || "{}");
        var project = ave_project(params);
        var xml = new File(params.xml_path || "");
        if (!xml.exists || !/\.xml$/i.test(xml.fsName)) throw new Error("XML not found: " + params.xml_path);
        if (!params.sequence_name) throw new Error("sequence_name is required");
        if (!/^[A-Za-z0-9_-]{8,100}$/.test(params.operation_id || "")) throw new Error("A stable operation_id is required");
        var folder = new Folder(params.operation_dir || "");
        if (!folder.exists) throw new Error("operation_dir must exist");
        var prepared = new File(folder.fsName + "/" + params.operation_id + ".prepared.json");
        var completed = new File(folder.fsName + "/" + params.operation_id + ".completed.json");
        var fingerprint = JSON.stringify({ project: ave_path(project.path), xml: ave_path(xml.fsName),
            bytes: xml.length, modified: String(xml.modified), sha256: params.xml_sha256 || "", name: params.sequence_name });
        if (completed.exists) {
            var previous = ave_read(completed);
            if (previous.fingerprint !== fingerprint) throw new Error("OPERATION_CONFLICT: operation_id was used for another input");
            ave_findSequence(project, {sequence_id: previous.data.imported_sequence_id});
            previous.data.replayed = true;
            return previous.data;
        }
        if (prepared.exists) throw new Error("OUTCOME_UNKNOWN: prior import was started; inspect the project before retrying");
        var before = ave_sequences(project); var ids = {};
        for (var i = 0; i < before.length; i++) {
            if (before[i].name === params.sequence_name) throw new Error("SEQUENCE_NAME_EXISTS: " + params.sequence_name);
            ids[before[i].id] = true;
        }
        ave_writeNew(prepared, {fingerprint: fingerprint, before: before, project_path: project.path, created_at: Date.now()});
        // importFiles returns a boolean, NOT an array of ProjectItems.
        if (!project.importFiles([xml.fsName], true, project.rootItem, false)) throw new Error("Timeline import failed");
        var added = []; var selected = null;
        for (var j = 0; j < project.sequences.numSequences; j++) {
            var seq = project.sequences[j];
            if (!ids[seq.sequenceID]) {
                added.push({id: seq.sequenceID, name: seq.name});
                if (seq.name === params.sequence_name) selected = seq;
            }
        }
        if (added.length !== 1 || !selected) throw new Error("Import must add exactly the requested sequence; actual=" + JSON.stringify(added));
        var data = ave_state(project);
        data.imported_sequence_id = selected.sequenceID; data.imported_sequence_name = selected.name;
        data.added_sequences = added; data.replayed = false; data.operation_id = params.operation_id;
        ave_writeNew(completed, {fingerprint: fingerprint, data: data});
        return data;
    });
}

function rollback_project(paramsJson) {
    return ave_result(function () {
        var params = JSON.parse(paramsJson || "{}");
        var project = ave_project(params);
        var checkpoint = new File(params.checkpoint_path || "");
        var manifest = new File(checkpoint.fsName + ".ave-checkpoint.json");
        if (!checkpoint.exists || !manifest.exists) throw new Error("Checkpoint or manifest missing");
        var record = ave_read(manifest);
        if (ave_path(record.checkpoint_path) !== ave_path(checkpoint.fsName) || checkpoint.length !== record.file_size) throw new Error("Checkpoint verification failed");
        var recovery = ave_output(params.recovery_path, "prproj");
        var rc = project.saveAs(recovery.fsName);
        if (rc !== 0 && rc !== true) throw new Error("Cannot preserve current project before rollback");
        project = ensureProject();
        if (ave_path(project.path) !== ave_path(recovery.fsName) || !new File(recovery.fsName).exists) throw new Error("Recovery copy verification failed");
        // Suppress prompts only after saving all current work to the recovery copy.
        project.closeDocument(0, 0);
        app.openDocument(checkpoint.fsName, 1, 1, 1, 1);
        var restored = ensureProject();
        if (ave_path(restored.path) !== ave_path(checkpoint.fsName)) throw new Error("Rollback opened a different project; preserved at " + recovery.fsName);
        if (record.active_sequence_id && !restored.openSequence(record.active_sequence_id)) throw new Error("Cannot restore checkpoint's active sequence");
        if (JSON.stringify(ave_sequences(restored)) !== JSON.stringify(record.sequences)) throw new Error("Rollback sequence inventory differs from checkpoint");
        var data = ave_state(restored); data.rolled_back = true; data.recovery_path = recovery.fsName;
        data.checkpoint_path = checkpoint.fsName;
        return data;
    });
}
