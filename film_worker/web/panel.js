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
      else if (field) field.value = key === "profiles" ? JSON.stringify(value, null, 2) : value;
    }
    document.querySelector("#address").textContent = data.config.server || "本机控制面板";
    refreshProfiles();
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
    config.profiles = JSON.parse(config.profiles);
    config.auto_update = form.elements.namedItem("auto_update").checked;
    for (const field of form.querySelectorAll("input[type=number]")) config[field.name] = Number(field.value);
    await api("config", {config, token});
    form.elements.namedItem("token").value = "";
    document.querySelector("#address").textContent = config.server || "本机控制面板";
    refreshProfiles();
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
const choice = document.querySelector("#profile-choice");
const modelFields = {
  codex_provider: document.querySelector("#codex-provider"),
  codex_model: document.querySelector("#codex-model"),
  reasoning_effort: document.querySelector("#codex-effort"),
};
const draft = form.elements.namedItem("profiles");

function refreshProfiles() {
  const profiles = JSON.parse(draft.value || "[]");
  const index = Number(choice.value || 0);
  choice.replaceChildren(...profiles.map((profile, order) => {
    const option = document.createElement("option");
    option.value = order;
    option.textContent = `${profile.codex_model ?? "未配置"} · ${profile.profile_hash?.slice(0, 8) ?? "草稿"}`;
    return option;
  }));
  if (index < profiles.length) choice.value = index;
  selectProfile();
}

function selectProfile() {
  const profile = JSON.parse(draft.value || "[]")[Number(choice.value)];
  for (const [field, input] of Object.entries(modelFields)) input.value = profile?.[field] ?? "";
  for (const id of ["draft-model", "test-model"]) document.querySelector("#" + id).disabled = !profile;
}

draft.addEventListener("change", () => {
  try { refreshProfiles(); } catch (error) { message(error.message, true); }
});
choice.addEventListener("change", selectProfile);

document.querySelector("#draft-model").addEventListener("click", async () => {
  try {
    const profiles = JSON.parse(draft.value);
    const index = Number(choice.value);
    const model = Object.fromEntries(Object.entries(modelFields).map(([k, input]) => [k, input.value.trim()]));
    const changed = await api("model-draft", {base: profiles[index], model});
    profiles[index] = changed.profile;
    draft.value = JSON.stringify(profiles, null, 2);
    refreshProfiles();
    message("已生成新档案摘要。保存后需由管理员使用同一档案派发。");
  } catch (error) {
    message(error.message, true);
  }
});

document.querySelector("#test-model").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  message("正在测试本机 Codex 登录与模型");
  try {
    const profile = JSON.parse(draft.value)[Number(choice.value)];
    const response = await api("model-test", {profile_hash: profile.profile_hash});
    const fields = Object.values(response.observed).filter(Boolean).join(" · ");
    message(fields ? "模型测试通过 · " + fields : "调用已通过，CLI 未提供实际模型标识");
  } catch (error) {
    message(error.message, true);
  } finally {
    button.disabled = false;
  }
});

openPanel();
