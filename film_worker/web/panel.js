const form = document.querySelector("#form");
const notice = document.querySelector("#notice");
let csrf = "";

function message(text, error = false) {
  notice.textContent = text;
  notice.dataset.error = error;
}

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Panel-Key": csrf},
    body: JSON.stringify(body)
  };
  const res = await fetch("/api/" + path, init);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error ?? ("HTTP " + res.status));
  return data;
}

const stateNames = {connected: "服务已连接", stopped: "未运行", connecting: "正在连接", network_unavailable: "网络不可用", configuration_error: "配置需处理"};
const stages = {download: "正在下载输入", codex_author: "Codex 制作", prepare_source: "抽取原片帧", compare_review: "生成对比图", verify_review: "核对审核签名"};

async function refresh() {
  try {
    const state = await api("status");
    const badge = document.querySelector("#connection");
    badge.textContent = stateNames[state.connection] ?? state.connection;
    badge.title = state.detail ?? "";
    badge.dataset.online = state.connection === "connected";
    const release = state.release;
    document.querySelector("#update-status").textContent = release ?
      `${release.version} · ${release.commit.slice(0, 12) || "本地版本"} · ${state.update_status}` : "";
    document.querySelector('[data-control="rollback"]').disabled = !state.managed || !state.rollback_available;
    document.querySelector('[data-control="update"]').disabled = !state.managed;
    const counts = state.slots ? `${state.active.length} / ${state.slots} 个槽位 · 剩余 ${state.free_gib} GiB` : "节点待启动";
    document.querySelector("#capacity").textContent = counts + (state.draining ? " · 暂停领单" : "") +
      (state.detail ? " · " + state.detail : "");
    const list = document.querySelector("#jobs");
    const rows = state.active.map(job => {
      const row = document.createElement("li");
      const left = document.createElement("strong");
      left.textContent = `${job.work_id} · 尝试 ${job.attempt}`;
      const right = document.createElement("span");
      right.textContent = (stages[job.stage] ?? job.stage) + (job.total == null ? "" : ` ${job.completed} / ${job.total}`);
      row.append(left, right);
      return row;
    });
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.textContent = "当前没有运行中的工单";
      rows.push(empty);
    }
    list.replaceChildren(...rows);
    const recent = (state.history ?? []).map(item => {
      const line = document.createElement("li");
      const transfer = item.delivery_versions.length ? ` · 已交付版 ${item.delivery_versions.join("、")}` : "";
      line.textContent = `${item.job_id} · ${item.kind} · ${item.job_state ?? item.status}${transfer}`;
      return line;
    });
    document.querySelector("#history").replaceChildren(...recent);
  } catch (error) {
    document.querySelector("#connection").textContent = error.message;
  }
}

async function openPanel() {
  try {
    const data = await api("config");
    csrf = data.csrf;
    for (const [key, value] of Object.entries(data.config)) {
      const field = form.elements.namedItem(key);
      if (field?.type === "checkbox") field.checked = value;
      else if (field) field.value = value;
    }
    document.querySelector("#address").textContent = data.config.server || "本机控制面板";
  } catch (error) {
    message(error.message, true);
  }
  await refresh();
  window.setInterval(refresh, 2500);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const config = Object.fromEntries(new FormData(form));
    const token = config.token;
    delete config.token;
    config.auto_update = form.elements.namedItem("auto_update").checked;
    for (const field of form.querySelectorAll("input[type=number]")) config[field.name] = Number(field.value);
    await api("config", {config, token});
    form.elements.namedItem("token").value = "";
    document.querySelector("#address").textContent = config.server || "本机控制面板";
    message("配置已保存");
  } catch (error) {
    message(error.message, true);
  }
});

document.querySelectorAll("[data-control]").forEach(button => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const state = await api(button.dataset.control, {});
      message(state.versions ? Object.entries(state.versions).map(([k, v]) => `${k} ${v}`).join(" · ") : "操作已提交");
      await refresh();
    } catch (error) {
      message(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
});
document.querySelector("#test-model").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  message("正在测试本机 Codex 登录与模型");
  try {
    const response = await api("model-test", {});
    const fields = Object.values(response.observed).filter(Boolean).join(" · ");
    message(fields ? "模型测试通过 · " + fields : "调用已通过，CLI 未提供实际模型标识");
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
});

const loginStatus = document.querySelector("#login-status");
const loginToggle = document.querySelector("#login-toggle");
let loginEnabled = false;

function showLogin(data) {
  loginEnabled = data.enabled;
  loginStatus.textContent = loginEnabled ? "已启用 · 下次登录后后台运行" : "未启用";
  loginToggle.textContent = loginEnabled ? "关闭登录启动" : "启用登录启动";
}

async function loadLogin() {
  try {
    const status = await api("status");
    const data = await api("login");
    showLogin(data);
    loginToggle.disabled = !status.managed || !csrf;
  } catch (error) {
    loginStatus.textContent = error.message;
  }
}

loginToggle.addEventListener("click", async () => {
  loginToggle.disabled = true;
  try {
    showLogin(await api("login", {enabled: !loginEnabled}));
  } catch (error) {
    loginStatus.textContent = error.message;
  } finally {
    loginToggle.disabled = false;
  }
});

openPanel().then(loadLogin);
