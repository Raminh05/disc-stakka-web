/* Optional progressive enhancement. Nothing here is required.
 *
 * The PS3's NetFront has a crippled DOM — reportedly no properties on
 * `document` — so every access is feature-detected and the whole file is
 * wrapped so a parse or runtime failure can never break the page. The server
 * already emits a working <meta http-equiv="refresh"> for job pages; this only
 * makes the poll smoother where XHR actually works.
 */
(function () {
    "use strict";

    if (!document || !document.getElementById || !window.XMLHttpRequest) { return; }

    var meta = null, tags = document.getElementsByTagName("meta"), i;
    for (i = 0; i < tags.length; i++) {
        if ((tags[i].getAttribute("http-equiv") || "").toLowerCase() === "refresh") {
            meta = tags[i];
        }
    }
    if (!meta) { return; }  /* terminal job page, or not a job page at all */

    var match = /url=(.+)$/i.exec(meta.getAttribute("content") || "");
    if (!match) { return; }
    var url = match[1];

    /* Take over from meta refresh: poll JSON and only reload when the phase
       actually changes. Avoids the full-page flash every second. */
    meta.parentNode.removeChild(meta);

    var phase = null;
    function poll() {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", url + ".json", true);
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) { return; }
            if (xhr.status !== 200) { window.location.href = url; return; }
            var data;
            try { data = JSON.parse(xhr.responseText); }
            catch (e) { window.location.href = url; return; }

            if (phase === null) { phase = data.phase; }
            if (data.phase !== phase || data.done) { window.location.href = url; return; }

            var el = document.getElementById("countdown");
            if (el && data.remaining !== null) {
                el.firstChild.nodeValue = data.remaining + " second"
                    + (data.remaining === 1 ? "" : "s") + " left";
            }
            window.setTimeout(poll, 1000);
        };
        try { xhr.send(null); } catch (e) { window.location.href = url; }
    }
    window.setTimeout(poll, 1000);
}());
