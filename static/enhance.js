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

    if (!document || !document.getElementById || !window.XMLHttpRequest || !window.JSON) { return; }

    var meta = null, phase = null, tags = document.getElementsByTagName("meta"), i;
    for (i = 0; i < tags.length; i++) {
        if ((tags[i].getAttribute("http-equiv") || "").toLowerCase() === "refresh") {
            meta = tags[i];
        } else if (tags[i].getAttribute("name") === "job-phase") {
            phase = tags[i].getAttribute("content");
        }
    }
    if (!meta || !phase) { return; }  /* terminal job page, or not a job page at all */

    var match = /url=(.+)$/i.exec(meta.getAttribute("content") || "");
    if (!match) { return; }
    var url = match[1];

    /* Poll JSON and reload once the job leaves the phase this page was
       rendered with. Removing the meta tag here would not help: the parser has
       already scheduled its refresh. Instead each JSON reply sets a short-lived
       cookie telling the server this browser can poll, and from the next load
       it sends a slow refresh as a safety net only. The first poll goes out at
       once so the cookie is in place before a 1 s refresh fires. A poll that
       fails is simply tried again a second later: the meta refresh is the
       safety net, and reloading on error would loop as fast as the server
       could answer. A 404 is not that kind of failure: the server no longer
       knows the job, and the page it sends instead is not a job page, so
       going there cannot loop. */
    function later() { window.setTimeout(poll, 1000); }

    function setText(id, text) {
        var el = document.getElementById(id);
        if (el && el.firstChild) { el.firstChild.nodeValue = text; }
    }

    function poll() {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", url + ".json", true);
        xhr.timeout = 5000;  /* a stalled poll reports status 0 below */
        xhr.onreadystatechange = function () {
            if (xhr.readyState !== 4) { return; }
            if (xhr.status === 404) { window.location.href = url; return; }
            if (xhr.status !== 200) { later(); return; }
            var data;
            try { data = JSON.parse(xhr.responseText); }
            catch (e) { later(); return; }

            if (data.phase !== phase || data.done) { window.location.href = url; return; }

            if (data.message) { setText("message", data.message); }
            if (data.remaining !== null) {
                setText("countdown", data.remaining + " second"
                    + (data.remaining === 1 ? "" : "s") + " left");
            }
            later();
        };
        try { xhr.send(null); } catch (e) { later(); }
    }
    poll();
}());
