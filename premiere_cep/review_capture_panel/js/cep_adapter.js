/* SPDX-License-Identifier: MIT
 * Minimal adapter for the public CEP native host. This is not Adobe's SDK file.
 * Only the three calls used by these panels are exposed.
 */
(function (root) {
    "use strict";
    function host() {
        if (!root.__adobe_cep__) { throw new Error("This panel requires an Adobe CEP host."); }
        return root.__adobe_cep__;
    }
    function PanelHost() {}
    PanelHost.prototype.evalScript = function (script, callback) {
        return host().evalScript(script, typeof callback === "function" ? callback : function () {});
    };
    PanelHost.prototype.getSystemPath = function (kind) {
        var value = String(host().getSystemPath(kind));
        if (/^file:\/\/\/[A-Za-z]:/.test(value)) { value = value.slice(8); }
        else if (/^file:\/\//.test(value)) { value = value.slice(7); }
        try { return decodeURI(value); } catch (_error) { return value; }
    };
    PanelHost.prototype.log = function (message) {
        if (root.console && root.console.log) { root.console.log(String(message)); }
    };
    root.SystemPath = { EXTENSION: "extension" };
    root.CSInterface = PanelHost;
}(typeof window !== "undefined" ? window : this));
