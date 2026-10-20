/* Read-only Premiere location adapter for the review capture panel. */
if (typeof $._AECapture === "undefined") { $._AECapture = {}; }
(function () {
    var TICKS_PER_SECOND = 254016000000;
    // Premiere ExtendScript does not provide the browser's JSON global.
    function jsonQuote(value) {
        var input = String(value), output = '"';
        for (var i = 0; i < input.length; i++) {
            var code = input.charCodeAt(i), ch = input.charAt(i);
            if (ch === '"') { output += '\\"'; }
            else if (ch === '\\') { output += '\\\\'; }
            else if (code < 32) {
                var hex = code.toString(16);
                output += '\\u' + ('0000' + hex).slice(-4);
            } else { output += ch; }
        }
        return output + '"';
    }
    function jsonEncode(value) {
        if (value === null || value === undefined) { return 'null'; }
        if (typeof value === 'string') { return jsonQuote(value); }
        if (typeof value === 'number') { return isFinite(value) ? String(value) : 'null'; }
        if (typeof value === 'boolean') { return value ? 'true' : 'false'; }
        var parts = [], i, key;
        if (value instanceof Array) {
            for (i = 0; i < value.length; i++) { parts.push(jsonEncode(value[i])); }
            return '[' + parts.join(',') + ']';
        }
        if (typeof value === 'object') {
            for (key in value) {
                if (Object.prototype.hasOwnProperty.call(value, key)) {
                    parts.push(jsonQuote(key) + ':' + jsonEncode(value[key]));
                }
            }
            return '{' + parts.join(',') + '}';
        }
        return 'null';
    }

    function safe(value, fallback) { try { return value; } catch (e) { return fallback; } }
    function str(value, fallback) { return value === undefined || value === null ? (fallback || "") : String(value); }
    function asTicks(time) {
        if (!time) { return null; }
        var value = safe(time.ticks, null);
        return value === null ? null : String(value);
    }
    function gcd(a, b) {
        a = Math.abs(a); b = Math.abs(b);
        while (b) { var t = a % b; a = b; b = t; }
        return a || 1;
    }
    function frameRateFromTimebase(timebase) {
        var frameTicks = Number(timebase);
        if (!isFinite(frameTicks) || frameTicks <= 0 || Math.floor(frameTicks) !== frameTicks) { return null; }
        var divisor = gcd(TICKS_PER_SECOND, frameTicks);
        return { numerator: TICKS_PER_SECOND / divisor, denominator: frameTicks / divisor, frame_duration_ticks: String(frameTicks) };
    }
    function clipValue(clip, field) { return safe(clip[field], null); }
    function getClipSnapshot(clip, trackType, trackIndex, clipIndex) {
        var start = clipValue(clip, "start");
        var end = clipValue(clip, "end");
        var inPoint = clipValue(clip, "inPoint");
        var outPoint = clipValue(clip, "outPoint");
        var item = clipValue(clip, "projectItem");
        return {
            track_type: trackType,
            track_index: trackIndex,
            clip_index: clipIndex,
            name: str(clipValue(clip, "name"), ""),
            start_ticks: asTicks(start),
            end_ticks: asTicks(end),
            source_in_ticks: asTicks(inPoint),
            source_out_ticks: asTicks(outPoint),
            project_item_name: item ? str(safe(item.name, ""), "") : null,
            project_item_node_id: item ? str(safe(item.nodeId, ""), "") : null
        };
    }
    function collectTracks(seq) {
        var clips = [], complete = true, summary = [];
        var collections = [{ kind: "video", value: seq.videoTracks }, { kind: "audio", value: seq.audioTracks }];
        for (var c = 0; c < collections.length; c++) {
            var collection = collections[c].value;
            if (!collection) { complete = false; continue; }
            var trackCount = Number(safe(collection.numTracks, 0));
            summary.push({ type: collections[c].kind, count: trackCount });
            for (var t = 0; t < trackCount; t++) {
                try {
                    var track = collection[t];
                    var trackClips = track.clips;
                    var clipCount = Number(trackClips.numItems);
                    summary.push({ type: collections[c].kind, index: t, clip_count: clipCount, name: str(safe(track.name, ""), "") });
                    for (var i = 0; i < clipCount; i++) {
                        clips.push(getClipSnapshot(trackClips[i], collections[c].kind, t, i));
                    }
                } catch (e) { complete = false; summary.push({ type: collections[c].kind, index: t, read_error: String(e) }); }
            }
        }
        return { complete: complete, summary: summary, clips: clips };
    }
    function contentFingerprint(snapshotText) {
        var a = 2166136261, b = 5381;
        for (var i = 0; i < snapshotText.length; i++) {
            var code = snapshotText.charCodeAt(i);
            a = ((a ^ code) * 16777619) >>> 0;
            b = (((b << 5) + b) ^ code) >>> 0;
        }
        function hex(value) { return ("00000000" + value.toString(16)).slice(-8); }
        return "fnv32x2:" + hex(a) + hex(b);
    }
    function isoNow() {
        var d = new Date();
        function pad(v) { return v < 10 ? "0" + v : String(v); }
        return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + "T" +
            pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + "Z";
    }
    function readPosition(seq, settings) {
        var pos = seq.getPlayerPosition();
        var ticks = asTicks(pos);
        if (ticks === null || !/^-?\d+$/.test(ticks)) { throw new Error("Premiere 没有返回精确 ticks"); }
        var label = null;
        try { label = String(pos.getFormatted(settings.videoFrameRate, seq.videoDisplayFormat)); } catch (e) {}
        return { ticks: ticks, time_label: label || ("ticks " + ticks), seconds: safe(pos.seconds, null) };
    }
    function rawContext() {
        if (!app.project) { throw new Error("Premiere 当前没有打开工程"); }
        var project = app.project;
        var seq = project.activeSequence;
        if (!seq) { throw new Error("Premiere 当前没有活动序列"); }
        if (!project.path) { throw new Error("请先保存 Premiere 工程，再记录意见"); }
        var originalProjectPath = str(project.path);
        var originalSequenceId = str(seq.sequenceID);
        var settings = seq.getSettings();
        var before = readPosition(seq, settings);
        var timeline = collectTracks(seq);
        var selection = [];
        try {
            var selected = seq.getSelection() || [];
            for (var i = 0; i < selected.length; i++) { selection.push(getClipSnapshot(selected[i], "selected", -1, i)); }
        } catch (selectionError) { selection = null; }
        var inTime = safe(seq.getInPointAsTime(), null);
        var outTime = safe(seq.getOutPointAsTime(), null);
        var after = readPosition(seq, settings);
        if (before.ticks !== after.ticks) { throw new Error("播放头仍在移动，请停稳后再记录"); }
        var currentProject = app.project;
        var currentSequence = currentProject ? currentProject.activeSequence : null;
        if (!currentSequence || str(currentProject.path) !== originalProjectPath ||
            str(currentSequence.sequenceID) !== originalSequenceId) {
            throw new Error("读取期间工程或序列发生变化");
        }
        var rate = frameRateFromTimebase(seq.timebase);
        var canonical = jsonEncode({ sequence_id: str(seq.sequenceID), timebase: str(seq.timebase), zero_point: str(seq.zeroPoint), tracks: timeline.summary, clips: timeline.clips });
        return {
            read_at: isoNow(),
            project: { name: str(project.name), path: str(project.path), host_id: str(safe(project.documentID, "")) || null },
            sequence: {
                name: str(seq.name), host_id: str(safe(seq.sequenceID, "")) || null,
                timebase: str(seq.timebase), zero_point: str(seq.zeroPoint),
                end_ticks: asTicks(safe(seq.end, null)),
                frame_rate: rate,
                display_format: safe(seq.videoDisplayFormat, null),
                audio_track_count: safe(seq.audioTracks.numTracks, null),
                video_track_count: safe(seq.videoTracks.numTracks, null)
            },
            location: before,
            selection: selection,
            in_out: { in_ticks: asTicks(inTime), out_ticks: asTicks(outTime) },
            timeline_snapshot: { tracks: timeline.summary, clips: timeline.clips },
            content_fingerprint: contentFingerprint(canonical),
            content_fingerprint_status: timeline.complete ? "partial" : "incomplete",
            content_fingerprint_coverage: ["audio_video_track_counts", "clip_names", "sequence_bounds", "source_in_out"]
        };
    }
    function encode(fn) {
        try { return jsonEncode({ ok: true, data: fn() }); }
        catch (e) { return jsonEncode({ ok: false, error: String(e) }); }
    }

    $._AECapture.__reviewBridgeVersion = '1.2.6';
    $._AECapture.captureStableSnapshot = function () { return encode(rawContext); };
    $._AECapture.getPlayheadPreview = function () {
        try {
            if (!app.project || !app.project.activeSequence) { return jsonEncode({ ok: false, error: "没有活动序列" }); }
            var seq = app.project.activeSequence;
            return jsonEncode({ ok: true, data: {
                project_path: str(app.project.path), sequence_id: str(seq.sequenceID),
                sequence_name: str(seq.name), position: readPosition(seq, seq.getSettings())
            } });
        } catch (e) { return jsonEncode({ ok: false, error: String(e) }); }
    };
    $._AECapture.seekToTicks = function (ticks) {
        try {
            if (!/^-?\d+$/.test(String(ticks))) { throw new Error("ticks 格式无效"); }
            if (!app.project || !app.project.activeSequence) { throw new Error("没有活动序列"); }
            app.project.activeSequence.setPlayerPosition(String(ticks));
            return jsonEncode({ ok: true, data: true });
        } catch (e) { return jsonEncode({ ok: false, error: String(e) }); }
    };
}());
