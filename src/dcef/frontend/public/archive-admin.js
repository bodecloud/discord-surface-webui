(function () {
  const DM_KEY_STORAGE = "discordRo.dmApiKey";
  const API = "/api/archive";

  const originalFetch = window.fetch.bind(window);
  window.fetch = function patchedFetch(input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    const dmKey = sessionStorage.getItem(DM_KEY_STORAGE);
    if (dmKey && url && (url.startsWith("/api/") || url.startsWith(`${location.origin}/api/`))) {
      init = init ? { ...init } : {};
      const headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has("x-dm-api-key")) headers.set("x-dm-api-key", dmKey);
      init.headers = headers;
    }
    return originalFetch(input, init);
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of children || []) node.append(child);
    return node;
  }

  async function api(path, options) {
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers: {
        "content-type": "application/json",
        ...(options && options.headers ? options.headers : {}),
      },
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      throw new Error((data && data.detail) || `HTTP ${response.status}`);
    }
    return data;
  }

  function setBusy(button, busy, label) {
    button.disabled = busy;
    button.innerHTML = busy ? `<span class="spin"></span> ${label || "Working"}` : button.dataset.label;
  }

  function renderItemList(container, items, emptyText, render) {
    container.replaceChildren();
    if (!items.length) {
      container.append(el("div", { class: "muted", text: emptyText }));
      return;
    }
    items.forEach((item) => container.append(render(item)));
  }

  function init() {
    if (document.getElementById("archiveAdminRoot")) return;
    const root = el("div", { id: "archiveAdminRoot" });
    const panel = el("section", { class: "archive-admin" });
    const sourceList = el("div", { class: "list" });
    const backupList = el("div", { class: "list" });
    const remoteList = el("div", { class: "list" });
    const jobList = el("div", { class: "list" });
    const refreshJobList = el("div", { class: "list" });
    const liveList = el("div", { class: "list" });
    const status = el("div", { class: "status muted", text: "Loading archive status..." });

    const inviteInput = el("input", { placeholder: "Discord invite link or code" });
    const sourceButton = el("button", { class: "primary", text: "Add" });
    sourceButton.dataset.label = "Add";

    const refreshButton = el("button", { class: "primary", text: "Refresh Status" });
    refreshButton.dataset.label = "Refresh Status";
    const refreshAllButton = el("button", { class: "primary", text: "Refresh All" });
    refreshAllButton.dataset.label = "Refresh All";
    const authorizeButton = el("button", { text: "Authorize Discord" });
    authorizeButton.dataset.label = "Authorize Discord";

    const dmInput = el("input", { placeholder: "DM API key for this tab", type: "password" });
    dmInput.value = sessionStorage.getItem(DM_KEY_STORAGE) || "";
    const dmButton = el("button", { text: "Set DM Key" });
    dmButton.dataset.label = "Set DM Key";

    const providerInput = el("select", {}, [
      el("option", { value: "rclone", text: "rclone remote" }),
      el("option", { value: "s3", text: "S3-compatible (stored only)" }),
      el("option", { value: "webdav", text: "WebDAV" }),
    ]);
    const remoteName = el("input", { placeholder: "New rclone remote name" });
    const remoteType = el("select", {}, [
      el("option", { value: "s3", text: "S3-compatible" }),
      el("option", { value: "webdav", text: "WebDAV" }),
      el("option", { value: "local", text: "Local path" }),
    ]);
    const remoteConfig = el("textarea", { placeholder: '{"provider":"Other","endpoint":"https://s3.example.com","access_key_id":"...","secret_access_key":"..."}' });
    const remoteButton = el("button", { class: "primary", text: "Connect Remote" });
    remoteButton.dataset.label = "Connect Remote";
    const backupName = el("input", { placeholder: "Backup target name" });
    const backupConfig = el("textarea", { placeholder: '{"remote":"gdrive","path":"discord-ro"}' });
    const backupButton = el("button", { class: "primary", text: "Save Backup Target" });
    backupButton.dataset.label = "Save Backup Target";

    async function load() {
      setBusy(refreshButton, true, "Refreshing");
      try {
        const [sources, refresh, oauth, discordConfig, backups, remotes, jobs, refreshJobs] = await Promise.all([
          api("/sources"),
          api("/refresh/status"),
          api("/discord/oauth/status"),
          api("/discord/config"),
          api("/backup-targets"),
          api("/backup-remotes"),
          api("/backup-jobs"),
          api("/refresh/jobs"),
        ]);
        const auth = refresh.authorization || {};
        status.textContent = [
          refresh.cache_policy,
          `Discord OAuth: ${auth.discord_oauth_configured ? "configured" : "missing"}.`,
          `OAuth user: ${oauth.authorized && oauth.user ? (oauth.user.global_name || oauth.user.username || "authorized") : "not authorized"}.`,
          `DiscordChatExporter: ${auth.discord_chat_exporter_available ? "installed" : "missing"}.`,
          `Live importer: ${auth.discord_importer_token_configured || auth.discord_bot_token_configured ? "connected" : "not connected"}.`,
          "Authorize Discord is the normal login path. Live refresh uses the server-side importer when available; anonymous mode still refreshes invite metadata and keeps cache intact.",
        ].join(" ");
        authorizeButton.disabled = false;

        renderItemList(sourceList, sources, "No enabled invite sources.", (source) => {
          const guild = source.metadata && source.metadata.guild;
          const title = guild && guild.name ? guild.name : source.code;
          const detail = source.live_refresh && source.live_refresh.detail ? source.live_refresh.detail : "No refresh status yet.";
          const counts = source.live_refresh && source.live_refresh.live_counts;
          const refreshOne = el("button", { text: "Refresh", onclick: () => refreshSource(source.code) });
          refreshOne.dataset.label = "Refresh";
          const showLive = el("button", { text: "Live", onclick: () => loadLiveMessages(source.code) });
          showLive.dataset.label = "Live";
          const removeOne = el("button", { class: "danger", text: "Remove", onclick: () => removeSource(source.code) });
          removeOne.dataset.label = "Remove";
          return el("div", { class: "item" }, [
            el("div", { class: "item-title", text: title }),
            el("div", { class: "muted", text: `Invite: ${source.code}` }),
            el("div", { class: "muted", text: detail }),
            el("div", { class: "muted", text: counts ? `Live cache: ${counts.messages} messages across ${counts.channels} channels.` : "Live cache: none yet." }),
            el("div", { class: "item-actions" }, [refreshOne, showLive, removeOne]),
          ]);
        });

        renderItemList(backupList, backups, "No backup targets configured.", (target) => {
          const runAll = el("button", { text: "Run", onclick: () => runBackup(target.name, "all", false) });
          runAll.dataset.label = "Run";
          const dryRun = el("button", { text: "Dry Run", onclick: () => runBackup(target.name, "all", true) });
          dryRun.dataset.label = "Dry Run";
          const removeTarget = el("button", { class: "danger", text: "Delete", onclick: () => deleteBackupTarget(target.name) });
          removeTarget.dataset.label = "Delete";
          return el("div", { class: "item" }, [
            el("div", { class: "item-title", text: target.name }),
            el("div", { class: "muted", text: `${target.provider}; ${target.enabled ? "enabled" : "disabled"}` }),
            el("div", { class: "item-actions" }, [runAll, dryRun, removeTarget]),
          ]);
        });

        const managedRemotes = new Set(remotes.managed_remotes || []);
        renderItemList(remoteList, remotes.remotes || [], "No rclone remotes visible.", (remote) => {
          const clean = String(remote).replace(/:$/, "");
          const actions = [];
          if (managedRemotes.has(clean)) {
            const removeRemote = el("button", { class: "danger", text: "Delete", onclick: () => deleteRemote(clean) });
            removeRemote.dataset.label = "Delete";
            actions.push(removeRemote);
          }
          return el("div", { class: "item" }, [
            el("div", { class: "item-title", text: clean }),
            el("div", { class: "muted", text: `Use {"remote":"${clean}","path":"discord-ro"} as a backup target config.` }),
            actions.length ? el("div", { class: "item-actions" }, actions) : el("div", { class: "muted", text: "Existing rclone remote." }),
          ]);
        });

        renderItemList(jobList, jobs, "No backup jobs have been started.", (job) => {
          const childText = (job.jobs || []).map((child) => {
            const rc = child.rclone_status || {};
            const state = rc.finished ? (rc.success ? "finished" : "failed") : "running";
            return `${child.source}: job ${child.jobid || "unknown"} ${state}`;
          }).join("; ");
          return el("div", { class: "item" }, [
            el("div", { class: "item-title", text: `${job.target} ${job.dry_run ? "dry run" : "backup"}` }),
            el("div", { class: "muted", text: childText || job.state || "pending" }),
            el("div", { class: "muted", text: job.cache_policy || "" }),
          ]);
        });

        renderItemList(refreshJobList, refreshJobs, "No refresh batches have run yet.", (job) => {
          const batch = job.batch || {};
          const batchText = batch.channel_count ? `batch ${batch.started_at_index + 1}/${batch.channel_count}, next ${batch.next_channel_index}` : "metadata/auth batch";
          return el("div", { class: "item" }, [
            el("div", { class: "item-title", text: `${job.source_code}: ${job.state || "queued"}` }),
            el("div", { class: "muted", text: batchText }),
            el("div", { class: "muted", text: job.detail || "" }),
          ]);
        });
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setBusy(refreshButton, false);
      }
    }

    async function addSource() {
      if (!inviteInput.value.trim()) return;
      setBusy(sourceButton, true, "Adding");
      try {
        await api("/sources", {
          method: "POST",
          body: JSON.stringify({ invite: inviteInput.value.trim() }),
        });
        inviteInput.value = "";
        await load();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setBusy(sourceButton, false);
      }
    }

    async function removeSource(code) {
      try {
        await api(`/sources/${encodeURIComponent(code)}`, { method: "DELETE" });
        await load();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function refreshSource(code) {
      try {
        await api(`/sources/${encodeURIComponent(code)}/refresh`, { method: "POST" });
        await load();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function refreshAllSources() {
      setBusy(refreshAllButton, true, "Refreshing");
      try {
        await api("/refresh/run", { method: "POST" });
        await load();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setBusy(refreshAllButton, false);
      }
    }

    async function loadLiveMessages(code) {
      try {
        const result = await api(`/sources/${encodeURIComponent(code)}/messages?limit=20`);
        const rows = result.messages || [];
        liveList.replaceChildren(el("div", { class: "status muted", text: `${result.mode}: ${result.detail}` }));
        if (!rows.length) {
          liveList.append(el("div", { class: "muted", text: "No messages available for that source." }));
          return;
        }
        rows.forEach((message) => {
          const author = message.author || {};
          liveList.append(el("div", { class: "item" }, [
            el("div", { class: "item-title", text: `${author.name || "Unknown author"} (${message.source})` }),
            el("div", { class: "muted", text: message.timestamp || "" }),
            el("div", { class: "live-content", text: message.content || "[message content unavailable]" }),
          ]));
        });
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function authorizeDiscord() {
      setBusy(authorizeButton, true, "Opening");
      try {
        const response = await fetch(`${API}/discord/oauth/start`, { redirect: "manual" });
        if (response.type === "opaqueredirect" || response.status === 0) {
          window.location.href = `${API}/discord/oauth/start`;
          return;
        }
        if (response.status >= 300 && response.status < 400) {
          const locationHeader = response.headers.get("location");
          window.location.href = locationHeader || `${API}/discord/oauth/start`;
          return;
        }
        const data = await response.json().catch(() => null);
        if (!response.ok) throw new Error((data && data.detail) || `HTTP ${response.status}`);
        window.location.href = `${API}/discord/oauth/start`;
      } catch (error) {
        status.textContent = error.message;
      } finally {
        setBusy(authorizeButton, false);
      }
    }

    async function createRemote() {
      if (!remoteName.value.trim()) return;
      let parameters = {};
      setBusy(remoteButton, true, "Connecting");
      try {
        if (remoteConfig.value.trim()) {
          parameters = JSON.parse(remoteConfig.value);
        }
        await api("/backup-remotes", {
          method: "POST",
          body: JSON.stringify({
            name: remoteName.value.trim(),
            type: remoteType.value,
            parameters,
          }),
        });
        remoteName.value = "";
        remoteConfig.value = "";
        await load();
      } catch (error) {
        status.textContent = error instanceof SyntaxError ? "Remote config must be valid JSON." : error.message;
      } finally {
        setBusy(remoteButton, false);
      }
    }

    async function runBackup(name, source, dryRun) {
      try {
        await api(`/backup-targets/${encodeURIComponent(name)}/run`, {
          method: "POST",
          body: JSON.stringify({ source, dry_run: dryRun }),
        });
        await load();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function deleteRemote(name) {
      try {
        await api(`/backup-remotes/${encodeURIComponent(name)}`, { method: "DELETE" });
        await load();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function deleteBackupTarget(name) {
      try {
        await api(`/backup-targets/${encodeURIComponent(name)}`, { method: "DELETE" });
        await load();
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function saveBackupTarget() {
      if (!backupName.value.trim()) return;
      let config = {};
      setBusy(backupButton, true, "Saving");
      try {
        if (backupConfig.value.trim()) {
          config = JSON.parse(backupConfig.value);
        }
        await api("/backup-targets", {
          method: "POST",
          body: JSON.stringify({
            name: backupName.value.trim(),
            provider: providerInput.value,
            enabled: true,
            config,
          }),
        });
        backupName.value = "";
        backupConfig.value = "";
        await load();
      } catch (error) {
        status.textContent = error instanceof SyntaxError ? "Backup config must be valid JSON." : error.message;
      } finally {
        setBusy(backupButton, false);
      }
    }

    dmButton.addEventListener("click", () => {
      const value = dmInput.value.trim();
      if (value) sessionStorage.setItem(DM_KEY_STORAGE, value);
      else sessionStorage.removeItem(DM_KEY_STORAGE);
      status.textContent = value ? "DM key set for this browser tab. Reload the archive list to include DM routes." : "DM key removed for this browser tab.";
    });
    sourceButton.addEventListener("click", addSource);
    refreshButton.addEventListener("click", load);
    refreshAllButton.addEventListener("click", refreshAllSources);
    authorizeButton.addEventListener("click", authorizeDiscord);
    remoteButton.addEventListener("click", createRemote);
    backupButton.addEventListener("click", saveBackupTarget);

    panel.append(
      el("header", {}, [
        el("h2", { text: "Archive Controls" }),
        el("div", { class: "muted", text: "Invite metadata is resolved anonymously. Live messages require Discord authorization." }),
      ]),
      el("div", { class: "body" }, [
        el("h3", { text: "Refresh" }),
        el("div", { class: "row" }, [refreshButton, refreshAllButton]),
        status,
        el("h3", { text: "Discord Connection" }),
        el("div", { class: "row" }, [authorizeButton]),
        el("h3", { text: "Invite Sources" }),
        el("div", { class: "row" }, [inviteInput, sourceButton]),
        el("div", { class: "status muted", text: "Removing a source hides it from this list; cached archive data is not deleted." }),
        sourceList,
        el("h3", { text: "DM Authorization" }),
        el("div", { class: "row" }, [dmInput, dmButton]),
        el("h3", { text: "Cloud Remotes" }),
        remoteName,
        remoteType,
        remoteConfig,
        el("div", { class: "row" }, [remoteButton]),
        remoteList,
        el("h3", { text: "Backup Targets" }),
        backupName,
        providerInput,
        backupConfig,
        el("div", { class: "row" }, [backupButton]),
        backupList,
        el("h3", { text: "Backup Jobs" }),
        jobList,
        el("h3", { text: "Refresh Batches" }),
        refreshJobList,
        el("h3", { text: "Live Messages" }),
        liveList,
      ])
    );

    const toggle = el("button", { class: "archive-admin-toggle", text: "Archive Controls" });
    toggle.addEventListener("click", () => {
      const open = root.getAttribute("data-open") === "true";
      root.setAttribute("data-open", open ? "false" : "true");
      if (!open) load();
    });
    root.append(panel, toggle);
    document.body.append(root);
    setInterval(() => {
      if (root.getAttribute("data-open") === "true") load();
    }, 30000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
