const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);

function show(el, text, isError = false) {
  el.textContent = text || "";
  el.classList.toggle("err", Boolean(isError));
}

function csv(value) {
  return String(value || "").split(/[,，;；\s]+/).filter(Boolean);
}

let defaults = {};

function fillDefaults(data) {
  defaults = data || {};
  $("def-kind").value = defaults.schedule_kind || "loop";
  $("def-value").value = defaults.schedule_value || "6h";
  $("def-file").value = defaults.file_type || "pdf";
  $("def-push").value = defaults.push_mode || "file";
  $("def-notice-type").value = defaults.notice_type || "group";
  $("def-sids").value = defaults.notice_sids || "";
  $("def-timing").value = defaults.notice_timing || "realtime";
  $("def-notice-cron").value = defaults.notice_cron || "0 9 * * *";
}

function renderSubscriptions(items) {
  const body = $("sub-list");
  if (!items || !items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">暂无订阅</td></tr>';
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td><code>JM${item.album_id}</code>${item.album_title ? "<br/>" + item.album_title : ""}</td>
      <td>${item.schedule_kind}<br/><code>${item.schedule_value}</code></td>
      <td>${item.file_type}</td>
      <td>${item.push_mode === "file" ? "文件+通知" : "仅通知"}</td>
      <td>${item.notice_type}<br/><code>${(item.targets || []).join("<br/>")}</code></td>
      <td>${item.next_run_at || "-"}</td>
      <td><button class="danger" data-album="${item.album_id}">删除</button></td>
    </tr>`).join("");
  body.querySelectorAll("button[data-album]").forEach((button) => {
    button.addEventListener("click", async () => {
      const albumId = button.getAttribute("data-album");
      await bridge.apiPost("subscriptions/delete", { album_id: albumId });
      await load();
    });
  });
}

async function load() {
  try {
    const data = await bridge.apiGet("subscriptions");
    fillDefaults(data.defaults);
    renderSubscriptions(data.subscriptions || []);
    const cfg = data.config || {};
    $("config-summary").textContent = Object.entries(cfg)
      .map(([key, value]) => `${key}: ${String(value)}`)
      .join(" · ");
  } catch (error) {
    show($("sub-out"), String(error), true);
  }
}

$("save-defaults").addEventListener("click", async () => {
  const payload = {
    schedule_kind: $("def-kind").value,
    schedule_value: $("def-value").value.trim(),
    file_type: $("def-file").value,
    push_mode: $("def-push").value,
    notice_type: $("def-notice-type").value,
    notice_sids: $("def-sids").value.trim(),
    notice_timing: $("def-timing").value,
    notice_cron: $("def-notice-cron").value.trim(),
  };
  try {
    const result = await bridge.apiPost("settings/save", payload);
    show($("defaults-out"), result.saved ? "默认值已保存" : "保存失败");
    await load();
  } catch (error) {
    show($("defaults-out"), String(error), true);
  }
});

$("save-sub").addEventListener("click", async () => {
  const targets = $("sub-targets").value
    .split(/\s+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const payload = {
    album_id: $("sub-album").value.trim(),
    album_title: "",
    schedule_kind: $("sub-kind").value,
    schedule_value: $("sub-value").value.trim(),
    file_type: $("sub-file").value,
    push_mode: $("sub-push").value,
    notice_type: $("sub-notice-type").value,
    targets,
    notice_timing: $("sub-timing").value,
    notice_cron: $("sub-notice-cron").value.trim(),
  };
  try {
    const result = await bridge.apiPost("subscriptions/save", payload);
    if (result && result.saved) {
      show($("sub-out"), "订阅已保存");
      await load();
    } else {
      show($("sub-out"), "保存失败", true);
    }
  } catch (error) {
    show($("sub-out"), String(error), true);
  }
});

load();
