/**
 * Ghost Write Tool — Backend (Node.js)
 * Provides API endpoints for the writing interface.
 */

"use strict";

const express = require("express");
const cors    = require("cors");
const multer  = require("multer");
const path    = require("path");
const jwt     = require("jsonwebtoken");
const { marked } = require("marked");

// ── Config ────────────────────────────────────────────────────────────────────

const GHOST_URL          = (process.env.GHOST_URL || "").replace(/\/$/, "");
const GHOST_API_KEY      = process.env.GHOST_API_KEY || "";
const PROXY_EMAIL_DOMAIN = process.env.PROXY_EMAIL_DOMAIN || "ghostproxy.internal";
const PORT               = parseInt(process.env.PORT || "5000", 10);
const UPLOAD_MAX_MB      = parseInt(process.env.UPLOAD_MAX_MB || "10", 10);

if (!GHOST_URL || !GHOST_API_KEY) {
    console.error("ERROR: GHOST_URL and GHOST_API_KEY are required.");
    process.exit(1);
}

// ── JWT ───────────────────────────────────────────────────────────────────────

function ghostToken() {
    const [kid, secretHex] = GHOST_API_KEY.split(":");
    return jwt.sign({}, Buffer.from(secretHex, "hex"), {
        keyid: kid, algorithm: "HS256", expiresIn: "5m", audience: "/admin/",
    });
}

function ghostHeaders() {
    return {
        "Authorization":  "Ghost " + ghostToken(),
        "Accept-Version": "v5.0",
        "Content-Type":   "application/json",
    };
}

// ── Ghost API ─────────────────────────────────────────────────────────────────

async function ghostGet(apiPath, params) {
    params = params || {};
    const url = new URL(GHOST_URL + "/ghost/api/admin/" + apiPath.replace(/^\//, ""));
    Object.keys(params).forEach(k => url.searchParams.set(k, params[k]));
    const resp = await fetch(url.toString(), { headers: ghostHeaders() });
    const data = await resp.json();
    if (!resp.ok) throw new Error(ghostErrorMsg(data, resp.status));
    return data;
}

async function ghostPost(apiPath, body) {
    const url  = GHOST_URL + "/ghost/api/admin/" + apiPath.replace(/^\//, "");
    const resp = await fetch(url, {
        method: "POST", headers: ghostHeaders(), body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(ghostErrorMsg(data, resp.status));
    return data;
}

function ghostErrorMsg(data, status) {
    const err = (data && data.errors && data.errors[0]) || {};
    const msg = err.message || ("HTTP " + status);
    const ctx = err.context || "";
    return ctx ? msg + ": " + ctx : msg;
}

// ── App ───────────────────────────────────────────────────────────────────────

const app    = express();
const upload = multer({
    storage: multer.memoryStorage(),
    limits:  { fileSize: UPLOAD_MAX_MB * 1024 * 1024 },
    fileFilter: function(_req, file, cb) {
        const allowed = ["image/jpeg", "image/png", "image/gif", "image/webp"];
        cb(null, allowed.includes(file.mimetype));
    },
});

function handleUpload(req, res, next) {
    upload.single("image")(req, res, function(err) {
        if (err && err.code === "LIMIT_FILE_SIZE")
            return res.status(400).json({ error: "Bild zu groß. Maximum ist " + UPLOAD_MAX_MB + " MB." });
        if (err)
            return res.status(400).json({ error: "Upload-Fehler: " + err.message });
        next();
    });
}

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, "static")));

function sendError(res, message, status) {
    status = status || 400;
    console.warn("[" + status + "] " + message);
    return res.status(status).json({ error: message });
}

// ── Routes ────────────────────────────────────────────────────────────────────

// GET /api/proxies
app.get("/api/proxies", async function(_req, res) {
    try {
        const data    = await ghostGet("users/", { limit: "all", status: "active" });
        const proxies = (data.users || [])
            .filter(function(u) { return (u.email || "").endsWith("@" + PROXY_EMAIL_DOMAIN); })
            .map(function(u) { return { id: u.id, name: u.name, slug: u.slug, avatar: u.profile_image || "" }; });
        res.json(proxies);
    } catch (err) {
        sendError(res, "Ghost API: " + err.message, 502);
    }
});

// GET /api/tags
app.get("/api/tags", async function(_req, res) {
    try {
        const data = await ghostGet("tags/", { limit: "all", order: "name asc" });
        const tags = (data.tags || []).map(function(t) { return { id: t.id, name: t.name, slug: t.slug }; });
        res.json(tags);
    } catch (err) {
        sendError(res, "Ghost API: " + err.message, 502);
    }
});

// POST /api/images
app.post("/api/images", handleUpload, async function(req, res) {
    if (!req.file) return sendError(res, "Keine Datei übermittelt.");
    try {
        const formData = new FormData();
        const blob     = new Blob([req.file.buffer], { type: req.file.mimetype });
        formData.append("file", blob, req.file.originalname || "image.jpg");
        formData.append("purpose", "image");

        const resp = await fetch(GHOST_URL + "/ghost/api/admin/images/upload/", {
            method: "POST",
            headers: { "Authorization": "Ghost " + ghostToken(), "Accept-Version": "v5.0" },
            body: formData,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(ghostErrorMsg(data, resp.status));

        const url = data.images && data.images[0] && data.images[0].url;
        if (!url) throw new Error("Kein Bild-URL in Antwort.");
        res.json({ url: url });
    } catch (err) {
        sendError(res, "Upload fehlgeschlagen: " + err.message, 502);
    }
});

// POST /api/posts
app.post("/api/posts", async function(req, res) {
    const title          = (req.body.title || "").trim();
    const markdown       = (req.body.markdown || "").trim();
    const authorId       = req.body.authorId;
    const tags           = req.body.tags || [];
    const featureImageUrl = req.body.featureImageUrl || "";
    const featureImageAlt = req.body.featureImageAlt || "";
    const status         = req.body.status || "draft";
    const publishedAt    = req.body.publishedAt || "";

    if (!title)    return sendError(res, "Titel ist erforderlich.");
    if (!markdown) return sendError(res, "Inhalt ist erforderlich.");
    if (!authorId) return sendError(res, "Proxy (Autor) ist erforderlich.");

    const html = marked.parse(markdown);

    const post = {
        title:   title,
        html:    html,
        status:  status === "scheduled" ? "scheduled" : status,
        authors: [{ id: authorId }],
        tags:    tags.map(function(t) { return { name: t }; }),
    };

    if (featureImageUrl) {
        post.feature_image     = featureImageUrl;
        post.feature_image_alt = featureImageAlt;
    }

    if (status === "scheduled" && publishedAt) {
        post.published_at = publishedAt;
    }

    try {
        const data    = await ghostPost("posts/?source=html", { posts: [post] });
        const created = data.posts && data.posts[0];
        if (!created) throw new Error("Kein Post in Antwort.");
        res.status(201).json({ id: created.id, url: created.url, status: created.status });
    } catch (err) {
        sendError(res, "Post fehlgeschlagen: " + err.message, 502);
    }
});

// ── Start ─────────────────────────────────────────────────────────────────────

app.listen(PORT, "0.0.0.0", function() {
    console.log("Ghost Write Tool running on port " + PORT);
    console.log("Ghost URL: " + GHOST_URL);
});
