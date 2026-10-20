// hostscript.jsx — ExtendScript for Adobe Premiere Pro
// Cuthere Bridge: executes commands sent from Hermes via file-based protocol.
//
// Called from main.js via CSInterface.evalScript(), each function receives
// a JSON string parameter and returns a JSON string result.

// ═══════════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════════

// ───────────────────────────────────────────────────────────────────────────
// JSON polyfill (2026-10-17): Premiere Pro's ExtendScript engine has no JSON
// global; everything below uses JSON.stringify / JSON.parse.
// ───────────────────────────────────────────────────────────────────────────
if (typeof JSON === "undefined") {
    JSON = (function () {
        function esc(str) {
            var out = "";
            for (var i = 0; i < str.length; i++) {
                var c = str.charAt(i);
                var code = str.charCodeAt(i);
                if (c === '"') out += '\\"';
                else if (c === "\\") out += "\\\\";
                else if (c === "\n") out += "\\n";
                else if (c === "\r") out += "\\r";
                else if (c === "\t") out += "\\t";
                else if (code < 32) {
                    var hex = code.toString(16);
                    while (hex.length < 4) hex = "0" + hex;
                    out += "\\u" + hex;
                } else out += c;
            }
            return '"' + out + '"';
        }
        function stringify(value) {
            if (value === null) return "null";
            var t = typeof value;
            if (t === "undefined") return "null";
            if (t === "number") return isFinite(value) ? String(value) : "null";
            if (t === "boolean") return String(value);
            if (t === "string") return esc(value);
            if (value instanceof Array) {
                var parts = [];
                for (var i = 0; i < value.length; i++) parts.push(stringify(value[i]));
                return "[" + parts.join(",") + "]";
            }
            if (t === "object") {
                var items = [];
                for (var k in value) {
                    if (value.hasOwnProperty && !value.hasOwnProperty(k)) continue;
                    var vt = typeof value[k];
                    if (vt === "undefined" || vt === "function") continue;
                    items.push(esc(k) + ":" + stringify(value[k]));
                }
                return "{" + items.join(",") + "}";
            }
            return "null";
        }
        function parse(text) {
            var i = 0;
            function error(m) { throw new Error("JSON.parse: " + m + " at " + i); }
            function skip() { while (i < text.length && " \t\r\n".indexOf(text.charAt(i)) >= 0) i++; }
            function expect(word) {
                if (text.substr(i, word.length) !== word) error("expected " + word);
                i += word.length;
            }
            function parseString() {
                var out = "";
                if (text.charAt(i) !== '"') error("expected string");
                i++;
                while (true) {
                    var c = text.charAt(i);
                    if (c === "") error("unterminated string");
                    if (c === '"') { i++; return out; }
                    if (c === "\\") {
                        i++;
                        var e = text.charAt(i);
                        if (e === "u") {
                            out += String.fromCharCode(parseInt(text.substr(i + 1, 4), 16));
                            i += 5;
                        } else {
                            if (e === "n") out += "\n";
                            else if (e === "t") out += "\t";
                            else if (e === "r") out += "\r";
                            else if (e === "b") out += "\b";
                            else if (e === "f") out += "\f";
                            else out += e;
                            i++;
                        }
                    } else { out += c; i++; }
                }
            }
            function parseNumber() {
                var start = i;
                while (i < text.length && "-+.eE0123456789".indexOf(text.charAt(i)) >= 0) i++;
                var num = parseFloat(text.substring(start, i));
                if (isNaN(num)) error("bad number");
                return num;
            }
            function parseArray() {
                var arr = [];
                i++;
                skip();
                if (text.charAt(i) === "]") { i++; return arr; }
                while (true) {
                    arr.push(parseValue());
                    skip();
                    var ch = text.charAt(i);
                    if (ch === ",") { i++; continue; }
                    if (ch === "]") { i++; return arr; }
                    error("expected , or ]");
                }
            }
            function parseObject() {
                var obj = {};
                i++;
                skip();
                if (text.charAt(i) === "}") { i++; return obj; }
                while (true) {
                    skip();
                    var key = parseString();
                    skip();
                    if (text.charAt(i) !== ":") error("expected :");
                    i++;
                    obj[key] = parseValue();
                    skip();
                    var ch = text.charAt(i);
                    if (ch === ",") { i++; continue; }
                    if (ch === "}") { i++; return obj; }
                    error("expected , or }");
                }
            }
            function parseValue() {
                skip();
                var c = text.charAt(i);
                if (c === "{") return parseObject();
                if (c === "[") return parseArray();
                if (c === '"') return parseString();
                if (c === "t") { expect("true"); return true; }
                if (c === "f") { expect("false"); return false; }
                if (c === "n") { expect("null"); return null; }
                return parseNumber();
            }
            var result = parseValue();
            skip();
            if (i < text.length) error("trailing garbage");
            return result;
        }
        return { stringify: stringify, parse: parse };
    })();
}

function jsonResponse(success, data, error) {
    var resp = { success: success, timestamp: Date.now() };
    if (data !== undefined) resp.data = data;
    if (error) resp.error = error;
    return JSON.stringify(resp);
}

function ensureProject() {
    if (!app.project) {
        throw new Error("No project open in Premiere Pro");
    }
    return app.project;
}

// main.js loads this file and project_lifecycle.jsx by explicit extension paths.

// ═══════════════════════════════════════════════════════════════════════════
// Command implementations
// ═══════════════════════════════════════════════════════════════════════════

/**
 * PING — verify the bridge is alive.
 * Called every 5s by the panel to confirm connectivity.
 */
function ping(paramsJson) {
    try {
        return jsonResponse(true, {
            version: AVE_BRIDGE_VERSION,
            bridge: "Cuthere CEP Bridge",
            premiere_version: app.version,
            os: $.os,
            capabilities: ["import_timeline", "activate_sequence", "save_project", "create_checkpoint", "rollback_project"],
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * GET_PROJECT_INFO — return current Premiere project details.
 */
function get_project_info(paramsJson) {
    try {
        var proj = ensureProject();
        var sequences = [];
        for (var i = 0; i < proj.sequences.numSequences; i++) {
            var seq = proj.sequences[i];
            sequences.push({
                name: seq.name,
                id: seq.sequenceID,
                video_tracks: seq.videoTracks.numTracks,
                audio_tracks: seq.audioTracks.numTracks,
            });
        }

        return jsonResponse(true, {
            name: proj.name,
            path: proj.path,
            sequences: sequences,
            sequence_count: proj.sequences.numSequences,
            active_sequence_id: proj.activeSequence ? proj.activeSequence.sequenceID : null,
            active_sequence_name: proj.activeSequence ? proj.activeSequence.name : null,
            bridge_version: AVE_BRIDGE_VERSION,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * GET_SEQUENCE — return timeline data for a specific sequence.
 */
function get_sequence(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var proj = ensureProject();
        var seq = (params.sequence_id || params.sequence_name) ? ave_findSequence(proj, params) : proj.activeSequence;
        if (!seq) return jsonResponse(false, null, "No active sequence");
        var clips = [], tracks = [], allCount = 0;
        var ticksPerFrame = Number(seq.timebase);
        for (var kindIndex = 0; kindIndex < 2; kindIndex++) {
            var kind = kindIndex === 0 ? "video" : "audio";
            var collection = kindIndex === 0 ? seq.videoTracks : seq.audioTracks;
            for (var t = 0; t < collection.numTracks; t++) {
                var track = collection[t], items = [], muted = null;
                try { muted = Boolean(track.isMuted()); } catch (ignored) {}
                for (var c = 0; c < track.clips.numItems; c++) {
                    var clip = track.clips[c], mediaPath = "";
                    try { mediaPath = clip.projectItem.getMediaPath(); } catch (ignored2) {}
                    var item = { name: clip.name, node_id: clip.nodeId,
                        start: clip.start.seconds, end: clip.end.seconds,
                        in_point: clip.inPoint.seconds, out_point: clip.outPoint.seconds,
                        start_ticks: clip.start.ticks, end_ticks: clip.end.ticks,
                        in_ticks: clip.inPoint.ticks, out_ticks: clip.outPoint.ticks,
                        duration: clip.duration.seconds, track_index: t,
                        media_path: mediaPath, disabled: Boolean(clip.disabled) };
                    items.push(item); allCount++;
                    if (kind === "video") clips.push(item);
                }
                tracks.push({kind: kind, index: t, muted: muted, clips: items});
            }
        }
        return jsonResponse(true, {
            sequence_name: seq.name, sequence_id: seq.sequenceID,
            clip_count: clips.length, clips: clips, tracks: tracks, total_clip_count: allCount,
            frame_rate: ticksPerFrame ? 254016000000 / ticksPerFrame : null,
            ticks_per_frame: String(seq.timebase), player_position_ticks: seq.getPlayerPosition().ticks
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * ADD_CLIP — import and place a clip on the timeline.
 * Params: { clip_path: "C:/video.mp4", position: 5.0, track_index: 0 }
 */
function add_clip(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var clipPath = params.clip_path;
        var position = params.position || 0;
        var trackIndex = params.track_index || 0;

        var proj = ensureProject();
        var seq = proj.activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        // Import file
        var file = new File(clipPath);
        if (!file.exists) {
            return jsonResponse(false, null, "File not found: " + clipPath);
        }

        var imported = proj.importFiles([clipPath]);
        if (!imported || imported.length === 0) {
            return jsonResponse(false, null, "Failed to import: " + clipPath);
        }

        // Place on timeline
        var track = seq.videoTracks[trackIndex];
        var time = seq.createTime(position);
        track.insertClip(imported[0], time);

        return jsonResponse(true, {
            clip_name: imported[0].name,
            position: position,
            track_index: trackIndex,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * REMOVE_CLIP — remove a clip from the timeline by name.
 * Params: { clip_id: "clip_name" }
 */
function remove_clip(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var clipId = params.clip_id;

        var seq = ensureProject().activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        var found = false;
        for (var t = 0; t < seq.videoTracks.numTracks; t++) {
            var track = seq.videoTracks[t];
            for (var c = track.clips.numItems - 1; c >= 0; c--) {
                if (track.clips[c].name === clipId) {
                    track.clips[c].remove(0, 0); // remove video + audio
                    found = true;
                }
            }
        }

        return jsonResponse(true, {
            removed: found,
            clip_id: clipId,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * TRIM_CLIP — adjust a clip's in/out points.
 * Params: { clip_id: "clip_name", start_delta: 1.0, end_delta: -0.5 }
 */
function trim_clip(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var clipId = params.clip_id;
        var startDelta = params.start_delta || 0;
        var endDelta = params.end_delta || 0;

        var seq = ensureProject().activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        var found = false;
        for (var t = 0; t < seq.videoTracks.numTracks; t++) {
            var track = seq.videoTracks[t];
            for (var c = 0; c < track.clips.numItems; c++) {
                var clip = track.clips[c];
                if (clip.name === clipId) {
                    // Adjust start (trim in)
                    if (startDelta !== 0) {
                        clip.start = seq.createTime(clip.start.seconds + startDelta);
                    }
                    // Adjust end (trim out)
                    if (endDelta !== 0) {
                        clip.end = seq.createTime(clip.end.seconds + endDelta);
                    }
                    found = true;
                }
            }
        }

        return jsonResponse(true, {
            trimmed: found,
            clip_id: clipId,
            start_delta: startDelta,
            end_delta: endDelta,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * REPLACE_CLIP — replace a clip's source file.
 * Params: { old_clip_id: "old.mp4", new_clip_path: "C:/new.mp4", keep_properties: true }
 */
function replace_clip(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var oldClipId = params.old_clip_id;
        var newClipPath = params.new_clip_path;

        var proj = ensureProject();
        var seq = proj.activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        // Import new file
        var imported = proj.importFiles([newClipPath]);
        if (!imported || imported.length === 0) {
            return jsonResponse(false, null, "Failed to import: " + newClipPath);
        }

        // Find and replace
        var found = false;
        for (var t = 0; t < seq.videoTracks.numTracks; t++) {
            var track = seq.videoTracks[t];
            for (var c = 0; c < track.clips.numItems; c++) {
                var clip = track.clips[c];
                if (clip.name === oldClipId || clip.projectItem.name === oldClipId) {
                    clip.projectItem = imported[0];
                    found = true;
                }
            }
        }

        return jsonResponse(true, {
            replaced: found,
            old_clip_id: oldClipId,
            new_clip_path: newClipPath,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * ADD_MARKER — add a timeline marker.
 * Params: { position: 10.0, label: "Cut here", color: "blue" }
 */
function add_marker(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var position = params.position || 0;
        var label = params.label || "";
        var color = params.color || "blue";

        var seq = ensureProject().activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        var time = seq.createTime(position);
        var marker = seq.markers.createMarker(time);
        marker.name = label;
        marker.comments = label;

        return jsonResponse(true, {
            marker_added: true,
            position: position,
            label: label,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

/**
 * EXPORT_XML — export current sequence as FCP7 XML.
 * Params: { output_path: "C:/output.xml" }
 */
function export_xml(paramsJson) {
    try {
        var params = JSON.parse(paramsJson || "{}");
        var outputPath = params.output_path;

        var seq = ensureProject().activeSequence;
        if (!seq) {
            return jsonResponse(false, null, "No active sequence");
        }

        if (!outputPath) {
            // Just return the path without writing — Hermes handles XML generation
            return jsonResponse(true, {
                sequence_name: seq.name,
                ready_for_export: true,
            });
        }

        // Use Premiere's built-in FCP7 XML export
        app.project.exportFinalCutProXML(outputPath, true);

        var outFile = new File(outputPath);
        return jsonResponse(true, {
            exported: outFile.exists,
            output_path: outputPath,
            file_size: outFile.exists ? outFile.length : 0,
        });
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// Command router — called by main.js via CSInterface.evalScript()
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Route a command to the appropriate handler.
 * Called as: routeCommand('ping', '{}')
 */
function routeCommand(command, paramsJson) {
    try {
        switch (command) {
            case "import_timeline":
                return import_timeline(paramsJson);
            case "activate_sequence":
                return activate_sequence(paramsJson);
            case "save_project":
                return save_project(paramsJson);
            case "create_checkpoint":
                return create_checkpoint(paramsJson);
            case "rollback_project":
                return rollback_project(paramsJson);
            case "ping":
                return ping(paramsJson);
            case "get_project_info":
                return get_project_info(paramsJson);
            case "get_sequence":
                return get_sequence(paramsJson);
            case "add_clip":
                return add_clip(paramsJson);
            case "remove_clip":
                return remove_clip(paramsJson);
            case "trim_clip":
                return trim_clip(paramsJson);
            case "replace_clip":
                return replace_clip(paramsJson);
            case "add_marker":
                return add_marker(paramsJson);
            case "export_xml":
                return export_xml(paramsJson);
            default:
                return jsonResponse(false, null, "Unknown command: " + command);
        }
    } catch (e) {
        return jsonResponse(false, null, e.toString());
    }
}
