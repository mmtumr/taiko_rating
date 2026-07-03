const API_BASE = "https://kinoko.zorua.cn/api/v1";

const state = {
  records: [],
  constants: new Map(),
  rated: [],
};

const els = {
  apiKeyInput: document.getElementById("apiKeyInput"),
  playerIdInput: document.getElementById("playerIdInput"),
  endpointSelect: document.getElementById("endpointSelect"),
  bestNInput: document.getElementById("bestNInput"),
  fetchButton: document.getElementById("fetchButton"),
  fetchStatus: document.getElementById("fetchStatus"),
  constantsInput: document.getElementById("constantsInput"),
  constantsStatus: document.getElementById("constantsStatus"),
  loadSampleButton: document.getElementById("loadSampleButton"),
  applyConstantsButton: document.getElementById("applyConstantsButton"),
  recalculateButton: document.getElementById("recalculateButton"),
  exportButton: document.getElementById("exportButton"),
  ratingValue: document.getElementById("ratingValue"),
  recordCount: document.getElementById("recordCount"),
  matchedCount: document.getElementById("matchedCount"),
  tableBody: document.getElementById("ratingTableBody"),
  canvas: document.getElementById("ratingCanvas"),
  imageStatus: document.getElementById("imageStatus"),
};

function chartKey(songNo, level) {
  return `${songNo}:${level}`;
}

function levelName(level) {
  return {
    1: "梅",
    2: "竹",
    3: "松",
    4: "鬼",
    5: "里",
  }[String(level)] ?? String(level);
}

function scoreBonus(score) {
  const rate = Math.max(0, Math.min(Number(score) / 1_000_000, 1));
  const anchors = [
    [0.7, -2.0],
    [0.75, -1.0],
    [0.8, 0.0],
    [0.9, 0.5],
    [0.95, 1.0],
    [0.98, 1.3],
    [1.0, 1.5],
  ];

  if (rate <= anchors[0][0]) return anchors[0][1];
  for (let i = 1; i < anchors.length; i += 1) {
    const [x1, y1] = anchors[i - 1];
    const [x2, y2] = anchors[i];
    if (rate <= x2) {
      const t = (rate - x1) / (x2 - x1);
      return y1 + (y2 - y1) * t;
    }
  }
  return anchors[anchors.length - 1][1];
}

function formatScore(value) {
  return Number(value || 0).toLocaleString("en-US");
}

function parseCsvLine(line) {
  const cells = [];
  let current = "";
  let quoted = false;

  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"' && line[i + 1] === '"') {
      current += '"';
      i += 1;
    } else if (ch === '"') {
      quoted = !quoted;
    } else if (ch === "," && !quoted) {
      cells.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  cells.push(current.trim());
  return cells;
}

function parseConstants(text) {
  const map = new Map();
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  for (const line of lines) {
    if (/^song_no\s*,/i.test(line)) continue;
    const [songNo, level, constant, title = ""] = parseCsvLine(line);
    const song = Number(songNo);
    const diff = Number(level);
    const chartConstant = Number(constant);
    if (!Number.isFinite(song) || !Number.isFinite(diff) || !Number.isFinite(chartConstant)) {
      continue;
    }
    map.set(chartKey(song, diff), {
      songNo: song,
      level: diff,
      constant: chartConstant,
      title,
    });
  }

  return map;
}

function normalizeHirobaScore(item) {
  const detail = item.song_detail ?? {};
  return {
    songNo: Number(item.song_no),
    level: Number(item.level),
    title: detail.song_name || detail.song_name_jp || `song ${item.song_no}`,
    genre: detail.type || "",
    highScore: Number(item.high_score ?? 0),
    good: Number(item.good_cnt ?? 0),
    ok: Number(item.ok_cnt ?? 0),
    ng: Number(item.ng_cnt ?? 0),
    combo: Number(item.combo_cnt ?? 0),
    date: item.highscore_datetime || item.update_datetime || "",
    raw: item,
  };
}

function normalizeKinokoScore(group) {
  const scores = Array.isArray(group.scoreInfo) ? group.scoreInfo : [];
  return scores.map((item) => ({
    songNo: Number(group.song_no ?? item.song_no),
    level: Number(group.level ?? item.level),
    title: group.title_cn || group.title || `song ${group.song_no ?? item.song_no}`,
    genre: group.genre || "",
    highScore: Number(item.high_score ?? 0),
    good: Number(item.good_cnt ?? 0),
    ok: Number(item.ok_cnt ?? 0),
    ng: Number(item.ng_cnt ?? 0),
    combo: Number(item.combo_cnt ?? 0),
    date: item.highscore_datetime || item.update_datetime || "",
    raw: item,
  }));
}

function normalizeApiResponse(payload, endpoint) {
  const scoreInfo = payload?.data?.playedRecords?.scoreInfo;
  if (!Array.isArray(scoreInfo)) return [];
  if (endpoint === "kinoko") {
    return scoreInfo.flatMap(normalizeKinokoScore);
  }
  return scoreInfo.map(normalizeHirobaScore);
}

function pickBestRecords(records) {
  const best = new Map();
  for (const record of records) {
    const key = chartKey(record.songNo, record.level);
    const current = best.get(key);
    if (!current || record.highScore > current.highScore) {
      best.set(key, record);
    }
  }
  return [...best.values()];
}

function calculateRating() {
  const bestN = Math.max(1, Math.min(Number(els.bestNInput.value) || 30, 100));
  const rated = pickBestRecords(state.records)
    .map((record) => {
      const constant = state.constants.get(chartKey(record.songNo, record.level));
      if (!constant) return null;
      const bonus = scoreBonus(record.highScore);
      const single = constant.constant + bonus;
      return {
        ...record,
        constant: constant.constant,
        constantTitle: constant.title,
        bonus,
        single,
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.single - a.single || b.highScore - a.highScore);

  state.rated = rated;
  const best = rated.slice(0, bestN);
  const total = best.length ? best.reduce((sum, item) => sum + item.single, 0) / best.length : 0;

  els.ratingValue.textContent = best.length ? total.toFixed(2) : "--";
  els.recordCount.textContent = String(pickBestRecords(state.records).length);
  els.matchedCount.textContent = String(rated.length);
  renderTable(best);
  renderCanvas(best, total, bestN);
}

function renderTable(rows) {
  if (!rows.length) {
    els.tableBody.innerHTML = '<tr><td colspan="7" class="empty-cell">没有可计算成绩</td></tr>';
    return;
  }

  els.tableBody.innerHTML = rows
    .map(
      (row, index) => `
        <tr>
          <td>${index + 1}</td>
          <td>${escapeHtml(row.title)}</td>
          <td><span class="level-badge">${levelName(row.level)}</span></td>
          <td>${row.constant.toFixed(1)}</td>
          <td>${formatScore(row.highScore)}</td>
          <td>${row.bonus >= 0 ? "+" : ""}${row.bonus.toFixed(2)}</td>
          <td>${row.single.toFixed(2)}</td>
        </tr>
      `,
    )
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function drawText(ctx, textValue, x, y, options = {}) {
  const {
    size = 28,
    weight = "400",
    color = "#25211e",
    align = "left",
    baseline = "alphabetic",
  } = options;
  ctx.font = `${weight} ${size}px "Microsoft YaHei", "Segoe UI", Arial, sans-serif`;
  ctx.fillStyle = color;
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.fillText(textValue, x, y);
}

function trimText(ctx, value, maxWidth) {
  const textValue = String(value);
  if (ctx.measureText(textValue).width <= maxWidth) return textValue;
  let out = textValue;
  while (out.length > 1 && ctx.measureText(`${out}...`).width > maxWidth) {
    out = out.slice(0, -1);
  }
  return `${out}...`;
}

function renderCanvas(rows, rating, bestN) {
  const canvas = els.canvas;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#fbfaf7";
  ctx.fillRect(0, 0, w, h);

  drawText(ctx, "Taiko Rating", 64, 92, { size: 52, weight: "700" });
  drawText(ctx, `Best ${bestN} / ${new Date().toLocaleDateString("zh-CN")}`, 64, 132, {
    size: 24,
    color: "#6d645c",
  });

  ctx.fillStyle = "#25211e";
  roundRect(ctx, 760, 52, 256, 112, 16);
  ctx.fill();
  drawText(ctx, rows.length ? rating.toFixed(2) : "--", 888, 103, {
    size: 42,
    weight: "700",
    color: "#ffffff",
    align: "center",
    baseline: "middle",
  });
  drawText(ctx, "TR", 888, 138, {
    size: 22,
    color: "#d8d0c7",
    align: "center",
    baseline: "middle",
  });

  const listTop = 210;
  const rowH = 48;
  drawText(ctx, "#", 64, listTop - 22, { size: 20, weight: "700", color: "#6d645c" });
  drawText(ctx, "曲名", 122, listTop - 22, { size: 20, weight: "700", color: "#6d645c" });
  drawText(ctx, "定数", 662, listTop - 22, { size: 20, weight: "700", color: "#6d645c" });
  drawText(ctx, "分数", 766, listTop - 22, { size: 20, weight: "700", color: "#6d645c" });
  drawText(ctx, "TR", 970, listTop - 22, { size: 20, weight: "700", color: "#6d645c" });

  if (!rows.length) {
    drawText(ctx, "等待成绩和定数", w / 2, h / 2, {
      size: 34,
      weight: "700",
      color: "#6d645c",
      align: "center",
      baseline: "middle",
    });
  }

  rows.slice(0, 24).forEach((row, index) => {
    const y = listTop + index * rowH;
    ctx.fillStyle = index % 2 === 0 ? "#ffffff" : "#f0f6f8";
    roundRect(ctx, 54, y - 30, 972, 40, 8);
    ctx.fill();
    drawText(ctx, String(index + 1).padStart(2, "0"), 76, y - 4, {
      size: 20,
      weight: "700",
      color: "#c84632",
    });
    drawText(ctx, trimText(ctx, row.title, 470), 122, y - 4, { size: 22, weight: "600" });
    drawText(ctx, levelName(row.level), 600, y - 4, { size: 18, color: "#6d645c" });
    drawText(ctx, row.constant.toFixed(1), 680, y - 4, { size: 20, weight: "700" });
    drawText(ctx, formatScore(row.highScore), 766, y - 4, { size: 20, color: "#6d645c" });
    drawText(ctx, row.single.toFixed(2), 970, y - 4, {
      size: 22,
      weight: "700",
      color: "#2d719d",
      align: "right",
    });
  });

  drawText(ctx, "score bonus caps at 1,000,000", 64, h - 64, {
    size: 20,
    color: "#6d645c",
  });
  els.imageStatus.textContent = rows.length ? "已生成" : "未生成";
}

function roundRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

async function fetchScores() {
  const token = els.apiKeyInput.value.trim();
  const playerId = els.playerIdInput.value.trim();
  const endpoint = els.endpointSelect.value;
  if (!token) {
    els.fetchStatus.textContent = "缺少 API Key";
    return;
  }

  const params = new URLSearchParams();
  if (playerId) params.set("player_id", playerId);
  const url = `${API_BASE}/scores/${endpoint}${params.toString() ? `?${params.toString()}` : ""}`;

  els.fetchButton.disabled = true;
  els.fetchStatus.textContent = "获取中";
  try {
    const resp = await fetch(url, {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(payload.detail || `HTTP ${resp.status}`);
    }
    state.records = normalizeApiResponse(payload, endpoint);
    els.fetchStatus.textContent = `已获取 ${state.records.length} 条记录`;
    calculateRating();
  } catch (err) {
    els.fetchStatus.textContent = err instanceof Error ? err.message : "获取失败";
  } finally {
    els.fetchButton.disabled = false;
  }
}

function applyConstants() {
  state.constants = parseConstants(els.constantsInput.value);
  els.constantsStatus.textContent = `已载入 ${state.constants.size} 张谱面定数`;
  calculateRating();
}

function loadSampleConstants() {
  els.constantsInput.value = [
    "song_no,level,const,title",
    "938,4,10.4,零之交响曲",
    "72,2,6.0,The Carnivorous Carnival",
    "1172,4,11.6,Destination 2F29",
    "1043,5,11.6,憎悪と醜悪の花束(裏)",
    "993,5,11.6,彁(裏)",
  ].join("\n");
  applyConstants();
}

function exportPng() {
  calculateRating();
  const link = document.createElement("a");
  link.download = `taiko-rating-${Date.now()}.png`;
  link.href = els.canvas.toDataURL("image/png");
  link.click();
}

els.fetchButton.addEventListener("click", fetchScores);
els.loadSampleButton.addEventListener("click", loadSampleConstants);
els.applyConstantsButton.addEventListener("click", applyConstants);
els.recalculateButton.addEventListener("click", calculateRating);
els.exportButton.addEventListener("click", exportPng);

renderCanvas([], 0, Number(els.bestNInput.value) || 30);
